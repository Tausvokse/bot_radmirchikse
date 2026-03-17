import ctypes
import random
import sys
import threading
import time

import cv2
import keyboard
import mss
import numpy as np
import pydirectinput
import pytesseract

# =====================================================================
# ФІКС МАСШТАБУВАННЯ WINDOWS (КРИТИЧНО ВАЖЛИВО ДЛЯ ПРАВИЛЬНИХ КООРДИНАТ)
# =====================================================================
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception as e:
    print(f"Не вдалося встановити DPI Awareness: {e}")

# =====================================================================
# ГЛОБАЛЬНІ НАЛАШТУВАННЯ ТА КОНФІГУРАЦІЯ
# =====================================================================

# Вказуємо шлях до встановленого Tesseract-OCR
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# --- Керування ---
HOTKEY_START = "f5"
HOTKEY_STOP = "f6"
HOTKEY_EXIT = "f8"

KEY_FORWARD = "w"
KEY_BACKWARD = "s"
KEY_LEFT = "a"
KEY_RIGHT = "d"
KEY_SCAN = "alt"
KEY_DIG = "ctrl"  # Лівий CTRL для копання
KEY_INVENTORY = "i"

# --- Логіка бота ---
TARGET_SIGNAL = 93  # Сигнал, при якому починаємо копати
LOW_BATTERY_LEVEL = 10  # Відсоток батареї, при якому йдемо в інвентар
DIG_WAIT_TIME = 12.0  # Час на анімацію копання
DEBUG_MODE = False  # Збереження картинок для налаштування координат

BASE_RESOLUTION = (1920, 1080)
MAX_OCR_SAMPLES = 3
OCR_RETRY_DELAY = 0.08


# =====================================================================
# АДАПТИВНА ГЕОМЕТРІЯ ЕКРАНУ
# =====================================================================

class AdaptiveLayout:
    """Масштабує робочі координати під будь-яку роздільну здатність екрану."""

    # Базові координати, з яких масштабуються регіони.
    SIGNAL_REGION_BASE = {"top": 800, "left": 1580, "width": 160, "height": 110}
    BATTERY_REGION_BASE = {"top": 730, "left": 1620, "width": 120, "height": 50}

    BATTERY_INV_BASE = (1000, 500)
    USE_BTN_BASE = (1050, 600)

    def __init__(self):
        self.screen_width, self.screen_height = pydirectinput.size()
        self.scale_x = self.screen_width / BASE_RESOLUTION[0]
        self.scale_y = self.screen_height / BASE_RESOLUTION[1]

        self.signal_region = self._scale_region(self.SIGNAL_REGION_BASE)
        self.battery_region = self._scale_region(self.BATTERY_REGION_BASE)
        self.battery_inv_point = self._scale_point(*self.BATTERY_INV_BASE)
        self.use_btn_point = self._scale_point(*self.USE_BTN_BASE)

    def _scale_region(self, region):
        return {
            "top": int(region["top"] * self.scale_y),
            "left": int(region["left"] * self.scale_x),
            "width": max(60, int(region["width"] * self.scale_x)),
            "height": max(35, int(region["height"] * self.scale_y)),
        }

    def _scale_point(self, x, y):
        return int(x * self.scale_x), int(y * self.scale_y)


# =====================================================================
# КЛАС ДЛЯ ЧИТАННЯ ЕКРАНУ (OCR)
# =====================================================================

class ScreenReader:
    def __init__(self, signal_region, battery_region):
        self.signal_region = signal_region
        self.battery_region = battery_region

    def preprocess_image(self, img_np, debug_name_prefix):
        """Підготовка зображення для кращого розпізнавання тексту."""
        img_bgr = cv2.cvtColor(img_np, cv2.COLOR_BGRA2BGR)

        if DEBUG_MODE:
            cv2.imwrite(f"{debug_name_prefix}_raw.png", img_bgr)

        img_gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        img_resized = cv2.resize(img_gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)

        # Адаптивний поріг краще переносить різні освітлення/гаму
        img_thresh = cv2.adaptiveThreshold(
            img_resized,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV,
            21,
            5,
        )

        # Легке морфологічне очищення від шуму.
        kernel = np.ones((2, 2), np.uint8)
        img_thresh = cv2.morphologyEx(img_thresh, cv2.MORPH_OPEN, kernel)

        if DEBUG_MODE:
            cv2.imwrite(f"{debug_name_prefix}_filtered.png", img_thresh)

        return img_thresh

    def _single_read(self, region, debug_name_prefix):
        with mss.mss() as sct:
            screenshot = sct.grab(region)
            img_np = np.array(screenshot)
            processed_img = self.preprocess_image(img_np, debug_name_prefix)

            custom_config = r"--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789"
            text = pytesseract.image_to_string(processed_img, config=custom_config)
            clean_text = "".join(filter(str.isdigit, text))

            return int(clean_text) if clean_text else -1

    def read_number(self, region, debug_name_prefix):
        """Скріншотить регіон і намагається стабільно прочитати з нього цифру."""
        values = []
        try:
            for _ in range(MAX_OCR_SAMPLES):
                value = self._single_read(region, debug_name_prefix)
                if value >= 0:
                    values.append(value)
                time.sleep(OCR_RETRY_DELAY)
        except Exception as e:
            print(f"[Помилка OCR]: {e}")
            return -1

        if not values:
            return -1

        # Беремо медіану для стійкості до випадкових OCR-збоїв.
        return int(np.median(values))

    def get_signal(self):
        return self.read_number(self.signal_region, "debug_signal")

    def get_battery(self):
        return self.read_number(self.battery_region, "debug_battery")


# =====================================================================
# ГОЛОВНИЙ КЛАС БОТА (АДАПТИВНИЙ ПОШУК)
# =====================================================================

class MetalDetectorBot:
    def __init__(self):
        self.is_running = False
        self.keep_alive = True
        self.bot_thread = None
        self.state = "IDLE"

        self.layout = AdaptiveLayout()
        self.reader = ScreenReader(self.layout.signal_region, self.layout.battery_region)

        # Адаптивний алгоритм пошуку
        self.last_signal = 0
        self.best_signal = 0
        self.search_mode = "SWEEP"
        self.sweep_direction = KEY_LEFT
        self.flat_signal_counter = 0

    def log(self, message):
        current_time = time.strftime("%H:%M:%S", time.localtime())
        print(f"[{current_time}] [{self.search_mode}] {message}")

    def release_all_keys(self):
        """Екстрене відпускання всіх можливих клавіш."""
        for key in [KEY_FORWARD, KEY_BACKWARD, KEY_LEFT, KEY_RIGHT, KEY_SCAN, KEY_DIG]:
            pydirectinput.keyUp(key)

    # --- Базові рухи ---
    def step_forward(self, duration=0.5):
        pydirectinput.keyDown(KEY_SCAN)
        pydirectinput.keyDown(KEY_FORWARD)
        time.sleep(duration)
        pydirectinput.keyUp(KEY_FORWARD)

    def step_backward(self, duration=0.5):
        pydirectinput.keyDown(KEY_SCAN)
        pydirectinput.keyDown(KEY_BACKWARD)
        time.sleep(duration)
        pydirectinput.keyUp(KEY_BACKWARD)

    def turn_character(self, direction_key, duration):
        pydirectinput.keyDown(direction_key)
        time.sleep(duration)
        pydirectinput.keyUp(direction_key)

    def zigzag_scan_step(self):
        """Плавний скан-рух: крок вперед + невеликий поворот."""
        self.step_forward(0.45)
        self.turn_character(self.sweep_direction, 0.16)

    def hard_recovery(self):
        """Відновлення, якщо довго немає прогресу."""
        self.log("Немає прогресу. Виконую глибокий розворот для перезапуску пошуку.")
        self.step_backward(0.35)
        self.turn_character(KEY_RIGHT, random.uniform(0.65, 0.95))
        self.last_signal = 0
        self.best_signal = 0
        self.flat_signal_counter = 0

    def dig_treasure(self):
        self.state = "DIGGING"
        self.log(f"СИГНАЛ {TARGET_SIGNAL}+! Зупиняюсь і копаю...")

        self.release_all_keys()
        time.sleep(0.4)

        pydirectinput.press(KEY_DIG)

        self.log(f"Чекаю {DIG_WAIT_TIME} секунд на завершення анімації копання...")
        wait_timer = 0.0
        while self.is_running and wait_timer < DIG_WAIT_TIME:
            time.sleep(1.0)
            wait_timer += 1.0

        self.log("Копання завершено. Продовжую рух.")
        self.last_signal = 0
        self.best_signal = 0
        self.search_mode = "SWEEP"
        self.state = "SEARCHING"

    def recharge_battery(self):
        self.state = "RECHARGING"
        self.log("Низький заряд батареї! Відкриваю інвентар...")

        self.release_all_keys()
        time.sleep(0.4)

        pydirectinput.press(KEY_INVENTORY)
        time.sleep(1.6)

        if not self.is_running:
            return

        battery_x, battery_y = self.layout.battery_inv_point
        use_x, use_y = self.layout.use_btn_point

        self.log("Клікаю на батарейки...")
        pydirectinput.moveTo(battery_x, battery_y)
        time.sleep(0.3)
        pydirectinput.click()
        time.sleep(0.6)

        self.log("Клікаю 'Использовать'...")
        pydirectinput.moveTo(use_x, use_y)
        time.sleep(0.3)
        pydirectinput.click()
        time.sleep(0.6)

        pydirectinput.press(KEY_INVENTORY)
        self.log("Батарейки замінено. Продовжую рух.")
        time.sleep(0.8)
        self.state = "SEARCHING"

    def adaptive_search_logic(self, current_signal):
        """Гібридний алгоритм: Sweep + Gradient Tracking + Recovery."""
        if current_signal < 0:
            self.log("OCR не дав валідне число. Короткий скан-рух.")
            self.zigzag_scan_step()
            return

        if current_signal >= TARGET_SIGNAL:
            self.dig_treasure()
            return

        if current_signal == 0:
            self.flat_signal_counter += 1
            self.log("Сигналу немає. Виконую sweep-пошук.")
            self.zigzag_scan_step()
            if self.flat_signal_counter % 5 == 0:
                self.sweep_direction = KEY_RIGHT if self.sweep_direction == KEY_LEFT else KEY_LEFT
            if self.flat_signal_counter >= 12:
                self.hard_recovery()
            return

        self.flat_signal_counter = 0
        self.best_signal = max(self.best_signal, current_signal)
        self.log(f"Сигнал: {current_signal} (попередній: {self.last_signal}, пік: {self.best_signal})")

        if self.last_signal == 0:
            # Перша зачіпка – дрібне наведення і вперед.
            self.turn_character(self.sweep_direction, 0.08)
            self.step_forward(0.42)
            self.last_signal = current_signal
            return

        delta = current_signal - self.last_signal
        if delta >= 3:
            # Стало суттєво краще: рухаємось впевнено вперед.
            self.step_forward(0.6)
        elif delta >= 0:
            # Невелике покращення/плато: повільний підбір кута.
            self.turn_character(self.sweep_direction, 0.1)
            self.step_forward(0.45)
        else:
            # Погіршення: пробуємо дзеркальний бік, якщо не допомагає – recovery.
            opposite = KEY_RIGHT if self.sweep_direction == KEY_LEFT else KEY_LEFT
            self.turn_character(opposite, 0.22)
            self.step_forward(0.35)
            self.sweep_direction = opposite

            if current_signal < max(1, self.best_signal - 12):
                self.hard_recovery()

        self.last_signal = current_signal

    def bot_loop(self):
        self.log("Бот активований! Починаю пошук металолому.")
        self.log(
            f"Роздільна здатність: {self.layout.screen_width}x{self.layout.screen_height}; "
            f"Signal region: {self.layout.signal_region}; Battery region: {self.layout.battery_region}"
        )
        self.state = "SEARCHING"

        while self.is_running:
            if self.state == "SEARCHING":
                current_signal = self.reader.get_signal()
                current_battery = self.reader.get_battery()

                if current_battery != -1 and current_battery <= LOW_BATTERY_LEVEL:
                    self.log(f"Заряд батареї: {current_battery}%")
                    self.recharge_battery()
                    continue

                self.adaptive_search_logic(current_signal)
                time.sleep(0.1)

        self.release_all_keys()
        self.log("Бот зупинений. Всі клавіші відпущено.")

    def start(self):
        if not self.is_running:
            self.is_running = True
            self.bot_thread = threading.Thread(target=self.bot_loop)
            self.bot_thread.start()
        else:
            self.log("Бот вже працює!")

    def stop(self):
        if self.is_running:
            self.log("Отримано команду на зупинку...")
            self.is_running = False
            if self.bot_thread:
                self.bot_thread.join()
        else:
            self.log("Бот не працює.")

    def exit_app(self):
        self.log("Вихід з програми...")
        self.stop()
        self.keep_alive = False
        sys.exit(0)


# =====================================================================
# ЗАПУСК ПРОГРАМИ
# =====================================================================
if __name__ == "__main__":
    bot = MetalDetectorBot()

    print("=====================================================")
    print(" 🎯 RADMIR METAL DETECTOR BOT (ADAPTIVE AUTO ALGORITHM) 🎯 ")
    print("=====================================================")
    print(f" [ {HOTKEY_START} ] - Запустити бота")
    print(f" [ {HOTKEY_STOP} ] - Зупинити бота (Пауза)")
    print(f" [ {HOTKEY_EXIT} ] - Вийти з програми")
    print("=====================================================")

    keyboard.add_hotkey(HOTKEY_START, bot.start)
    keyboard.add_hotkey(HOTKEY_STOP, bot.stop)
    keyboard.add_hotkey(HOTKEY_EXIT, bot.exit_app)

    try:
        while bot.keep_alive:
            time.sleep(1)
    except KeyboardInterrupt:
        bot.exit_app()
