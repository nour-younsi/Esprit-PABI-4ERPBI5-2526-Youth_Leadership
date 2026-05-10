import cv2
import mediapipe as mp
import pyautogui
import math
import time
import winsound
import numpy as np

# ======================
# SETUP
# ======================
cap = cv2.VideoCapture(0)
screen_w, screen_h = pyautogui.size()
pyautogui.FAILSAFE = False

mp_hands = mp.solutions.hands
hands = mp_hands.Hands(max_num_hands=1, min_detection_confidence=0.7)
mp_draw = mp.solutions.drawing_utils

# ======================
# SMOOTH CURSOR (PRO)
# ======================
prev_x, prev_y = 0, 0
alpha = 0.25

# ======================
# CLICK CONTROL
# ======================
last_click = 0
click_delay = 0.8

# ======================
# SCROLL CONTROL
# ======================
scroll_prev_y = 0
scroll_cooldown = 0

# ======================
# CALIBRATION (CENTER FIX)
# ======================
calib_x, calib_y = 1.0, 1.0

def dist(x1, y1, x2, y2):
    return math.hypot(x2 - x1, y2 - y1)

# ======================
# INTRO SCREEN
# ======================
intro_start = time.time()
while time.time() - intro_start < 2.5:
    frame = np.zeros((500, 900, 3), dtype=np.uint8)

    cv2.putText(frame, "AI HAND CONTROL ACTIVE", (120, 220),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 3)

    cv2.putText(frame, "Initializing System...", (170, 280),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

    cv2.imshow("AI HAND CONTROL SYSTEM", frame)
    cv2.waitKey(1)

# ======================
# MAIN LOOP
# ======================
while True:
    success, img = cap.read()
    img = cv2.flip(img, 1)
    h, w, _ = img.shape

    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    results = hands.process(rgb)

    mode = "CURSOR"
    action = "NONE"

    if results.multi_hand_landmarks:
        for handLms in results.multi_hand_landmarks:
            mp_draw.draw_landmarks(img, handLms, mp_hands.HAND_CONNECTIONS)
            lm = handLms.landmark

            # Fingers
            ix, iy = lm[8].x * w, lm[8].y * h
            tx, ty = lm[4].x * w, lm[4].y * h
            mx, my = lm[12].x * w, lm[12].y * h

            index_up = lm[8].y < lm[6].y
            middle_up = lm[12].y < lm[10].y

            scroll_mode = index_up and middle_up

            # ======================
            # SCROLL MODE
            # ======================
            if scroll_mode:
                mode = "SCROLL"

                current_y = lm[8].y

                if scroll_prev_y != 0:
                    diff = scroll_prev_y - current_y

                    if abs(diff) > 0.02:
                        now = time.time()
                        if now - scroll_cooldown > 0.05:
                            pyautogui.scroll(int(diff * 5000))
                            scroll_cooldown = now
                            action = "SCROLLING"

                scroll_prev_y = current_y

            # ======================
            # CURSOR MODE
            # ======================
            else:
                scroll_prev_y = 0
                mode = "CURSOR"

                target_x = lm[8].x * screen_w * calib_x
                target_y = lm[8].y * screen_h * calib_y

                curr_x = prev_x + alpha * (target_x - prev_x)
                curr_y = prev_y + alpha * (target_y - prev_y)

                pyautogui.moveTo(curr_x, curr_y)
                prev_x, prev_y = curr_x, curr_y

            # ======================
            # LEFT CLICK (thumb + index)
            # ======================
            if dist(ix, iy, tx, ty) < 30:
                now = time.time()
                if now - last_click > click_delay:
                    pyautogui.click()
                    winsound.Beep(800, 100)
                    last_click = now
                    action = "LEFT CLICK"

            # ======================
            # RIGHT CLICK (thumb + middle)
            # ======================
            if dist(tx, ty, mx, my) < 30:
                now = time.time()
                if now - last_click > click_delay:
                    pyautogui.rightClick()
                    winsound.Beep(600, 120)
                    last_click = now
                    action = "RIGHT CLICK"

    # ======================
    # FULL-SCREEN UI OVERLAY
    # ======================
    ui = np.zeros((h, w, 3), dtype=np.uint8)

    # semi-transparent overlay effect
    cv2.rectangle(ui, (0, 0), (400, 120), (0, 0, 0), -1)

    cv2.putText(ui, f"MODE: {mode}", (20, 50),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

    cv2.putText(ui, f"ACTION: {action}", (20, 95),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 255), 2)

    # combine camera + UI
    combined = cv2.addWeighted(img, 1, ui, 0.4, 0)

    cv2.imshow("AI HAND CONTROL SYSTEM (PRO)", combined)

    key = cv2.waitKey(1) & 0xFF
    if key == 27 or key == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
