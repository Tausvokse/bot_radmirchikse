import time
import threading
import sys
import random
import keyboard
import pydirectinput
import cv2
import numpy as np
import mss
import pytesseract
import ctypes

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
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

# --- Керування ---
HOTKEY_START = 'f5'
HOTKEY_STOP = 'f6'
HOTKEY_EXIT = 'f8'

KEY_FORWARD = 'w'
KEY_BACKWARD = 's'
KEY_LEFT = 'a'
KEY_RIGHT = 'd'
KEY_SCAN = 'alt'
KEY_DIG = 'ctrl'   # Лівий CTRL для копання
KEY_INVENTORY = 'i'

# --- Налаштування пошуку (Регіони екрану) ---
SIGNAL_REGION = {"top": 800, "left": 1580, "width": 160, "height": 110}
BATTERY_REGION = {"top": 730, "left": 1620, "width": 120, "height": 50}

# --- Налаштування інвентарю для батарейок ---
BATTERY_INV_X = 1000
BATTERY_INV_Y = 500
USE_BTN_X = 1050
USE_BTN_Y = 600

# --- Логіка бота ---
TARGET_SIGNAL = 93          # Сигнал, при якому починаємо копати
LOW_BATTERY_LEVEL = 10      # Відсоток батареї, при якому йдемо в інвентар
DIG_WAIT_TIME = 12.0        # Час на анімацію копання
DEBUG_MODE = True           # Збереження картинок для налаштування координат


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
        _, img_thresh = cv2.threshold(img_resized, 150, 255, cv2.THRESH_BINARY_INV)
        
        if DEBUG_MODE:
            cv2.imwrite(f"{debug_name_prefix}_filtered.png", img_thresh)
            
        return img_thresh

    def read_number(self, region, debug_name_prefix):
        """Скріншотить регіон і намагається прочитати з нього цифру."""
        try:
            with mss.mss() as sct:
                screenshot = sct.grab(region)
                img_np = np.array(screenshot)
                
                processed_img = self.preprocess_image(img_np, debug_name_prefix)
                
                custom_config = r'--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789'
                text = pytesseract.image_to_string(processed_img, config=custom_config)
                
                clean_text = ''.join(filter(str.isdigit, text))
                
                if clean_text:
                    return int(clean_text)
                return -1
                
        except Exception as e:
            print(f"[Помилка OCR]: {e}")
            return -1

    def get_signal(self):
        return self.read_number(self.signal_region, "debug_signal")

    def get_battery(self):
        return self.read_number(self.battery_region, "debug_battery")


# =====================================================================
# ГОЛОВНИЙ КЛАС БОТА (АЛГОРИТМ РАДАРНОГО ПОШУКУ)
# =====================================================================

class MetalDetectorBot:
    def __init__(self):
        self.is_running = False
        self.keep_alive = True
        self.bot_thread = None
        self.state = "IDLE"
        self.reader = ScreenReader(SIGNAL_REGION, BATTERY_REGION)
        
        # Змінні алгоритму розумного пошуку
        self.last_signal = 0
        self.search_mode = "FORWARD"  # Стани: FORWARD, RADAR_LEFT, RADAR_RIGHT

    def log(self, message):
        current_time = time.strftime("%H:%M:%S", time.localtime())
        print(f"[{current_time}] [{self.search_mode}] {message}")

    def release_all_keys(self):
        """Екстрене відпускання всіх можливих клавіш."""
        for key in [KEY_FORWARD, KEY_BACKWARD, KEY_LEFT, KEY_RIGHT, KEY_SCAN, KEY_DIG]:
            pydirectinput.keyUp(key)

    # --- Базові рухи ---
    def step_forward(self, duration=0.6):
        """Робить крок вперед для зчитування нового сигналу."""
        pydirectinput.keyDown(KEY_SCAN)
        pydirectinput.keyDown(KEY_FORWARD)
        time.sleep(duration)
        pydirectinput.keyUp(KEY_FORWARD)

    def step_backward(self, duration=0.6):
        """Робить крок назад, щоб повернутися на точку втраченого піку."""
        pydirectinput.keyDown(KEY_SCAN)
        pydirectinput.keyDown(KEY_BACKWARD)
        time.sleep(duration)
        pydirectinput.keyUp(KEY_BACKWARD)

    def turn_character(self, direction_key, duration):
        """Повертає персонажа на певний кут."""
        pydirectinput.keyDown(direction_key)
        time.sleep(duration)
        pydirectinput.keyUp(direction_key)

    def dig_treasure(self):
        """Процес викопування знахідки."""
        self.state = "DIGGING"
        self.log(f"СИГНАЛ {TARGET_SIGNAL}+! Зупиняюсь і копаю...")
        
        self.release_all_keys()
        time.sleep(0.5)
        
        pydirectinput.press(KEY_DIG)
        
        self.log(f"Чекаю {DIG_WAIT_TIME} секунд на завершення анімації копання...")
        wait_timer = 0.0
        while self.is_running and wait_timer < DIG_WAIT_TIME:
            time.sleep(1.0)
            wait_timer += 1.0
            
        self.log("Копання завершено. Продовжую рух.")
        self.last_signal = 0
        self.search_mode = "FORWARD"
        self.state = "SEARCHING"

    def recharge_battery(self):
        """Процес заміни батарейок через інвентар."""
        self.state = "RECHARGING"
        self.log("Низький заряд батареї! Відкриваю інвентар...")
        
        self.release_all_keys()
        time.sleep(0.5)
        
        pydirectinput.press(KEY_INVENTORY)
        time.sleep(2.0)
        
        if not self.is_running: return

        self.log("Клікаю на батарейки...")
        pydirectinput.moveTo(BATTERY_INV_X, BATTERY_INV_Y)
        time.sleep(0.5)
        pydirectinput.click()
        time.sleep(1.0)
        
        self.log("Клікаю 'Использовать'...")
        pydirectinput.moveTo(USE_BTN_X, USE_BTN_Y)
        time.sleep(0.5)
        pydirectinput.click()
        time.sleep(1.0)
        
        pydirectinput.press(KEY_INVENTORY)
        self.log("Батарейки замінено. Продовжую рух.")
        time.sleep(1.0)
        self.state = "SEARCHING"

    def advanced_search_logic(self, current_signal):
        """Розумний алгоритм Градієнтного спуску з Радарним скануванням."""
        
        # Якщо ми тільки почали або втратили сигнал
        if current_signal == 0:
            self.log("Сигналу немає (0). Шукаю зачіпку...")
            self.step_forward(1.0)
            if random.random() < 0.1:
                self.turn_character(random.choice([KEY_LEFT, KEY_RIGHT]), 0.3)
            self.last_signal = 0
            self.search_mode = "FORWARD"
            return

        self.log(f"Сигнал: {current_signal} (минулий: {self.last_signal})")

        if current_signal >= TARGET_SIGNAL:
            self.dig_treasure()
            return

        # ==============================================
        # МАШИНА СТАНІВ АЛГОРИТМУ
        # ==============================================
        if self.search_mode == "FORWARD":
            if current_signal > self.last_signal:
                self.log("Гаряче! Йду вірно, прямо.")
                self.last_signal = current_signal
                self.step_forward(0.6)
            elif current_signal < self.last_signal:
                self.log("Холодно! Пролетів ціль. Вмикаю РАДАР (Ліворуч).")
                self.search_mode = "RADAR_LEFT"
                self.step_backward(0.6) # Відступаємо на пік
                self.turn_character(KEY_LEFT, 0.3) # Тестовий поворот вліво
                self.step_forward(0.6) # Робимо крок у новому напрямку для зчитування
            else:
                self.step_forward(0.6) # Сигнал не змінився, робимо ще крок

        elif self.search_mode == "RADAR_LEFT":
            if current_signal > self.last_signal:
                self.log("Знайшов шлях ліворуч! Повертаюсь до прямого руху.")
                self.last_signal = current_signal
                self.search_mode = "FORWARD"
                self.step_forward(0.6)
            else:
                self.log("Ліворуч гірше. Пробую РАДАР (Праворуч).")
                self.search_mode = "RADAR_RIGHT"
                self.step_backward(0.6) # Знову відступаємо на пік
                # Компенсуємо минулий поворот вліво (0.3) і повертаємо вправо (0.3), разом = 0.6
                self.turn_character(KEY_RIGHT, 0.6) 
                self.step_forward(0.6) # Тестовий крок вправо

        elif self.search_mode == "RADAR_RIGHT":
            if current_signal > self.last_signal:
                self.log("Знайшов шлях праворуч! Повертаюсь до прямого руху.")
                self.last_signal = current_signal
                self.search_mode = "FORWARD"
                self.step_forward(0.6)
            else:
                self.log("Шлях повністю втрачено. Розвертаюсь для пошуку нової лінії.")
                self.step_backward(0.6)
                self.turn_character(KEY_RIGHT, 0.8) # Робимо сильний розворот, щоб не ходити колами
                self.search_mode = "FORWARD"
                self.last_signal = 0 # Скидаємо пам'ять, щоб почати "з чистого аркуша"


    def bot_loop(self):
        """Головний цикл бота."""
        self.log("Бот активований! Починаю пошук металолому.")
        self.state = "SEARCHING"
        
        while self.is_running:
            if self.state == "SEARCHING":
                current_signal = self.reader.get_signal()
                current_battery = self.reader.get_battery()
                
                # Перевірка батареї
                if current_battery != -1 and current_battery <= LOW_BATTERY_LEVEL:
                    self.log(f"Заряд батареї: {current_battery}%")
                    self.recharge_battery()
                    continue

                # Логіка руху
                if current_signal != -1:
                    self.advanced_search_logic(current_signal)
                else:
                    self.log("Не вдалося розпізнати сигнал. Залишаюсь на місці.")
                    time.sleep(0.5)
                
                # Мікро-пауза між ітераціями
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
    print(" 🎯 RADMIR METAL DETECTOR BOT (HILL CLIMB RADAR ALGORITHM) 🎯 ")
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