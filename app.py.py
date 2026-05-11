import os, time, threading
from datetime import date
import requests
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

# ═══════════════════════════════════════════════════
# 환경변수 (Render.com에서 설정)
# ═══════════════════════════════════════════════════
CLIENT_ID         = os.environ["CLIENT_ID"]
CLIENT_SECRET     = os.environ["CLIENT_SECRET"]
VIN               = os.environ["VIN"]
REDIRECT_URI      = os.environ["REDIRECT_URI"]
PROXY_URL         = os.environ["PROXY_URL"]
PIN               = os.environ["PIN"]
RENDER_API_KEY    = os.environ.get("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.environ.get("RENDER_SERVICE_ID", "")
NAVER_EMAIL        = os.environ.get("NAVER_EMAIL", "")
NAVER_APP_PASSWORD = os.environ.get("NAVER_APP_PASSWORD", "")

FLEET_API_URL  = "https://fleet-api.prd.na.vn.cloud.tesla.com"
TOKEN_URL      = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
SCOPES         = "openid vehicle_device_data vehicle_cmds vehicle_charging_cmds offline_access"

# ─── 스마트 공조 온도 설정 ───────────────────────────
TEMP_COOL         = 25.0
TEMP_HEAT         = 21.0   # 난방 단일 온도

VENT_OUT_LOW      = 20.0
VENT_OUT_HIGH     = 26.0
VENT_IN_COOL      = 26.0
VENT_IN_HEAT      = 20.0
TEMP_VENT_MIN     = 15.5

AFTERBLOW_SECONDS = 600
AFTERBLOW_TEMP    = 27.0
DEFAULT_TEMP      = 23.0

DELIVERY_LEVEL_PCT = 80.0
DELIVERY_RANGE_KM  = 496.0        # 출고 시 80% 기준 주행거리 (본인 차량에 맞게 수정)
DELIVERY_DATE      = date(2026, 1, 1)  # 출고일 (본인 차량에 맞게 수정)
MILES_TO_KM        = 1.60934

_afterblow_stop    = False
_afterblow_running = False

# ═══════════════════════════════════════════════════
# 브루트포스 차단
# ═══════════════════════════════════════════════════
_fail_count = 0
_lock_until  = 0
MAX_FAILS    = 5
LOCK_SECONDS = 3600

def check_pin():
    global _fail_count, _lock_until
    now  = time.time()
    data = request.json or {}
    if now < _lock_until:
        remain = int((_lock_until - now) / 60)
        raise PermissionError(f"잠금 상태 — {remain}분 후 다시 시도하세요")
    if data.get("pin") == PIN:
        _fail_count = 0
        return True
    _fail_count += 1
    if _fail_count >= MAX_FAILS:
        _lock_until = now + LOCK_SECONDS
        _fail_count = 0
        raise PermissionError(f"PIN {MAX_FAILS}회 오류 — 1시간 잠금")
    raise PermissionError(f"잘못된 PIN — {MAX_FAILS - _fail_count}회 남음")

# ═══════════════════════════════════════════════════
# 토큰 관리
# ═══════════════════════════════════════════════════
_token_cache = {
    "access_token":  None,
    "refresh_token": os.environ["TESLA_REFRESH_TOKEN"],
    "expires_at":    0
}
_token_expired = False

def get_access_token():
    global _token_expired
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 300:
        return _token_cache["access_token"]
    res = requests.post(TOKEN_URL, data={
        "grant_type":    "refresh_token",
        "client_id":     CLIENT_ID,
        "refresh_token": _token_cache["refresh_token"],
    })
    if res.status_code != 200:
        _token_expired = True
        raise Exception("TOKEN_EXPIRED")
    data = res.json()
    _token_cache["access_token"]  = data["access_token"]
    _token_cache["refresh_token"] = data["refresh_token"]
    _token_cache["expires_at"]    = now + data.get("expires_in", 28800)
    _token_expired = False
    threading.Thread(target=update_render_env, args=(data["refresh_token"],), daemon=True).start()
    return _token_cache["access_token"]

def update_render_env(new_refresh_token):
    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        return
    try:
        res = requests.get(
            f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars",
            headers={"Authorization": f"Bearer {RENDER_API_KEY}", "Accept": "application/json"}
        )
        if res.status_code != 200:
            return
        env_vars = res.json()
        updated = [
            {"key": v["envVar"]["key"],
             "value": new_refresh_token if v["envVar"]["key"] == "TESLA_REFRESH_TOKEN"
                      else v["envVar"]["value"]}
            for v in env_vars
        ]
        requests.put(
            f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars",
            headers={"Authorization": f"Bearer {RENDER_API_KEY}",
                     "Content-Type": "application/json"},
            json=updated
        )
    except Exception as e:
        import logging
        logging.warning(f"[render env update] {e}")

def h():
    return {"Authorization": f"Bearer {get_access_token()}", "Content-Type": "application/json"}

# ═══════════════════════════════════════════════════
# 차량 헬퍼
# ═══════════════════════════════════════════════════
def wake_vehicle():
    res = requests.get(f"{FLEET_API_URL}/api/1/vehicles/{VIN}", headers=h())
    if res.status_code == 200:
        if res.json().get("response", {}).get("state") == "online":
            return True
    requests.post(f"{FLEET_API_URL}/api/1/vehicles/{VIN}/wake_up", headers=h())
    for _ in range(30):
        time.sleep(2)
        res = requests.get(f"{FLEET_API_URL}/api/1/vehicles/{VIN}", headers=h())
        if res.status_code == 200:
            if res.json().get("response", {}).get("state") == "online":
                return True
    return False

def get_cached_battery():
    import logging
    res = requests.get(f"{FLEET_API_URL}/api/1/vehicles", headers=h())
    logging.warning(f"[battery] status: {res.status_code}")
    if res.status_code == 200:
        for v in res.json().get("response", []):
            if v.get("vin") == VIN:
                charge  = v.get("charge_state") or {}
                b_level = charge.get("battery_level")
                b_range = charge.get("battery_range")
                if b_level is not None and b_range is not None:
                    return {"battery_level": b_level, "battery_range": b_range,
                            "state": v.get("state", "unknown")}
    logging.warning("[battery] cache miss — waking vehicle")
    wake_vehicle()
    data    = get_vehicle_data()
    charge  = data.get("charge_state", {})
    b_level = charge.get("battery_level")
    b_range = charge.get("battery_range")
    if b_level is not None and b_range is not None:
        return {"battery_level": b_level, "battery_range": b_range,
                "state": "online", "woken": True}
    return None

def get_vehicle_data():
    res = requests.get(
        f"{FLEET_API_URL}/api/1/vehicles/{VIN}/vehicle_data",
        headers=h(),
        params={"endpoints": "climate_state;location_data;drive_state;charge_state"}
    )
    if res.status_code == 200:
        return res.json().get("response", {})
    return {}

def proxy_cmd(name, body=None):
    res = requests.post(
        f"{PROXY_URL}/api/1/vehicles/{VIN}/command/{name}",
        headers=h(), json=body or {}
    )
    if res.status_code == 200:
        return res.json().get("response", {}).get("result", False)
    return False

def get_climate_mode(outside_temp, inside_temp):
    # 환기 구간 (외부 20~26°C)
    if VENT_OUT_LOW <= outside_temp <= VENT_OUT_HIGH:
        if inside_temp >= VENT_IN_COOL:
            return "cool", TEMP_COOL, "냉방 중", f"실내 {inside_temp:.1f}°C 과열 → 냉방 {TEMP_COOL}°C"
        elif inside_temp <= VENT_IN_HEAT:
            return "heat", TEMP_HEAT, "난방 중", f"실내 {inside_temp:.1f}°C 냉기 → 난방 {TEMP_HEAT}°C"
        else:
            return "vent", inside_temp, "환기 중", f"실내 {inside_temp:.1f}°C 쾌적 → 송풍만"

    # 냉방 (외부 26°C 초과)
    if outside_temp > VENT_OUT_HIGH:
        return "cool", TEMP_COOL, "냉방 중", f"외부 {outside_temp:.1f}°C → 냉방 {TEMP_COOL}°C"

    # 난방 (외부 20°C 미만) — 단일 온도
    return "heat", TEMP_HEAT, "난방 중", f"외부 {outside_temp:.1f}°C → 난방 {TEMP_HEAT}°C"


def turn_off_all_heaters():
    """열선 전체 완전 OFF"""
    proxy_cmd("defrost_car", {"on": False})
    proxy_cmd("remote_auto_steering_wheel_heat_climate_request", {"auto_steering_wheel_heat": False})
    proxy_cmd("remote_steering_wheel_heater_request", {"on": False})
    proxy_cmd("remote_steering_wheel_heat_level_request", {"level": 0})
    proxy_cmd("remote_auto_seat_climate_request", {"auto_seat_position": 1, "enable": False})
    proxy_cmd("remote_auto_seat_climate_request", {"auto_seat_position": 2, "enable": False})
    proxy_cmd("remote_seat_heater_request", {"seat_position": 0, "level": 0})
    proxy_cmd("remote_seat_heater_request", {"seat_position": 1, "level": 0})
    proxy_cmd("remote_seat_heater_request", {"seat_position": 2, "level": 0})
    proxy_cmd("remote_seat_heater_request", {"seat_position": 4, "level": 0})

def send_naver_mail(subject, body):
    if not NAVER_EMAIL or not NAVER_APP_PASSWORD:
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = NAVER_EMAIL
        msg["To"]      = NAVER_EMAIL
        with smtplib.SMTP_SSL("smtp.naver.com", 465) as s:
            s.login(NAVER_EMAIL, NAVER_APP_PASSWORD)
            s.sendmail(NAVER_EMAIL, NAVER_EMAIL, msg.as_string())
    except Exception as e:
        import logging
        logging.warning(f"[mail] {e}")


# ═══════════════════════════════════════════════════
# PIN 검증
# ═══════════════════════════════════════════════════
@app.route("/api/verify", methods=["POST"])
def api_verify():
    try:
        check_pin()
        return jsonify({"ok": True})
    except PermissionError as e:
        return jsonify({"ok": False, "msg": str(e)})

# ═══════════════════════════════════════════════════
# 재인증 — Tesla 로그인 URL 생성
# ═══════════════════════════════════════════════════
@app.route("/api/reauth_url", methods=["POST"])
def api_reauth_url():
    try:
        check_pin()
    except PermissionError as e:
        return jsonify({"ok": False, "msg": str(e)}), 403
    import urllib.parse
    auth_url = (
        "https://auth.tesla.com/oauth2/v3/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&response_type=code"
        f"&scope={urllib.parse.quote(SCOPES)}"
        f"&state=reauth"
        f"&prompt=login"
    )
    return jsonify({"ok": True, "url": auth_url})

# ═══════════════════════════════════════════════════
# 재인증 — 콜백 URL 받아서 토큰 갱신
# ═══════════════════════════════════════════════════
@app.route("/api/reauth_code", methods=["POST"])
def api_reauth_code():
    try:
        check_pin()
    except PermissionError as e:
        return jsonify({"ok": False, "msg": str(e)}), 403
    try:
        data     = request.json or {}
        callback = data.get("callback_url", "")
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(callback)
        code   = parse_qs(parsed.query).get("code", [None])[0]
        if not code:
            return jsonify({"ok": False, "msg": "URL에서 code를 찾을 수 없습니다"})
        res = requests.post(TOKEN_URL, data={
            "grant_type":    "authorization_code",
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code":          code,
            "redirect_uri":  REDIRECT_URI,
            "audience":      FLEET_API_URL,
        })
        if res.status_code != 200:
            return jsonify({"ok": False, "msg": f"토큰 발급 실패: {res.text}"})
        token_data = res.json()
        _token_cache["access_token"]  = token_data["access_token"]
        _token_cache["refresh_token"] = token_data["refresh_token"]
        _token_cache["expires_at"]    = time.time() + token_data.get("expires_in", 28800)
        global _token_expired
        _token_expired = False
        threading.Thread(target=update_render_env,
                         args=(token_data["refresh_token"],), daemon=True).start()
        return jsonify({"ok": True, "msg": "재인증 완료 — 정상 작동합니다"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ═══════════════════════════════════════════════════
# 배터리 열화율
# ═══════════════════════════════════════════════════
@app.route("/api/battery", methods=["POST"])
def api_battery():
    try:
        check_pin()
    except PermissionError as e:
        return jsonify({"ok": False, "msg": str(e)}), 403
    try:
        cached = get_cached_battery()
        if cached is None:
            return jsonify({"ok": False, "msg": "배터리 데이터 없음 — 차량을 한 번 깨운 후 다시 시도하세요"})
        battery_level = cached.get("battery_level")
        battery_range = cached.get("battery_range")
        if not battery_level:
            return jsonify({"ok": False, "msg": "배터리 데이터 없음"})
        range_km       = battery_range * MILES_TO_KM
        range_at_80_km = range_km / battery_level * DELIVERY_LEVEL_PCT
        current_deg    = max(0.0, round((1 - range_at_80_km / DELIVERY_RANGE_KM) * 100, 2))
        today          = date.today()
        days_since     = max(1, (today - DELIVERY_DATE).days)
        daily_deg      = current_deg / days_since
        projections    = {}
        for yr in [1, 3, 5, 10]:
            pct  = round(daily_deg * yr * 365, 1)
            rng  = int(DELIVERY_RANGE_KM * (1 - pct / 100))
            projections[yr] = {"degradation": pct, "range_km": rng}
        return jsonify({
            "ok": True,
            "battery_level": battery_level,
            "range_km": round(range_km, 1),
            "range_at_80_km": round(range_at_80_km, 1),
            "delivery_range_km": DELIVERY_RANGE_KM,
            "current_degradation": current_deg,
            "daily_degradation": round(daily_deg, 4),
            "days_since_delivery": days_since,
            "vehicle_state": cached.get("state", "unknown"),
            "woken": cached.get("woken", False),
            "projections": projections,
        })
    except Exception as e:
        if "TOKEN_EXPIRED" in str(e):
            return jsonify({"ok": False, "msg": "토큰 만료", "need_reauth": True})
        return jsonify({"ok": False, "msg": str(e)})

# ═══════════════════════════════════════════════════
# 스마트 공조
# ═══════════════════════════════════════════════════
@app.route("/api/climate_set", methods=["POST"])
def api_climate_set():
    try:
        check_pin()
    except PermissionError as e:
        return jsonify({"ok": False, "msg": str(e)}), 403
    try:
        wake_vehicle()
        data    = get_vehicle_data()
        climate = data.get("climate_state", {})
        inside_temp  = climate.get("inside_temp")
        outside_temp = climate.get("outside_temp")
        if inside_temp is None or outside_temp is None:
            return jsonify({"ok": False, "msg": "온도 읽기 실패, 다시 시도하세요"})

        mode, target, status, reason = get_climate_mode(outside_temp, inside_temp)

        if mode == "vent":
            vent_target = max(TEMP_VENT_MIN, round(inside_temp, 1))
            proxy_cmd("auto_conditioning_start")
            proxy_cmd("set_temps", {"driver_temp": vent_target, "passenger_temp": vent_target})
            time.sleep(2)
            proxy_cmd("set_temps", {"driver_temp": vent_target, "passenger_temp": vent_target})
            ok = True
        else:
            vent_target = None
            proxy_cmd("set_temps", {"driver_temp": target, "passenger_temp": target})
            ok = proxy_cmd("auto_conditioning_start")

        time.sleep(2)

        # ─── 시트 열선 자동 제어 ───────────────────────────        
        if mode == "heat":
            # 자동 시트 제어 끄기
            proxy_cmd("remote_auto_seat_climate_request", {"auto_seat_position": 1, "auto_climate_on": False})
            proxy_cmd("remote_auto_seat_climate_request", {"auto_seat_position": 2, "auto_climate_on": False})
            time.sleep(2)
            proxy_cmd("remote_seat_heater_request", {"seat_position": 0, "level": 1})
            proxy_cmd("remote_seat_heater_request", {"seat_position": 1, "level": 2})
            time.sleep(2)
            proxy_cmd("remote_seat_heater_request", {"seat_position": 0, "level": 1})
            proxy_cmd("remote_seat_heater_request", {"seat_position": 1, "level": 2})
            proxy_cmd("remote_steering_wheel_heater_request", {"on": False})
            seat_heat_msg = "난방 모드 → 운전석 1단 / 조수석 2단"
        else:
            proxy_cmd("remote_seat_heater_request", {"seat_position": 0, "level": 0})
            proxy_cmd("remote_seat_heater_request", {"seat_position": 1, "level": 0})
            seat_heat_msg = ""

        return jsonify({
            "ok": ok,
            "running": True,
            "mode": mode,
            "status": status,
            "target": vent_target if mode == "vent" else target,
            "inside_temp": inside_temp,
            "outside_temp": outside_temp,
            "msg": reason + (f" / {seat_heat_msg}" if seat_heat_msg else "")
        })
    except Exception as e:
        if "TOKEN_EXPIRED" in str(e):
            return jsonify({"ok": False, "msg": "토큰 만료", "need_reauth": True})
        return jsonify({"ok": False, "msg": str(e)})

# ═══════════════════════════════════════════════════
# 스마트 공조 중단
# ═══════════════════════════════════════════════════
@app.route("/api/climate_stop", methods=["POST"])
def api_climate_stop():
    try:
        check_pin()
    except PermissionError as e:
        return jsonify({"ok": False, "msg": str(e)}), 403
    proxy_cmd("remote_seat_heater_request", {"seat_position": 0, "level": 0})
    proxy_cmd("remote_seat_heater_request", {"seat_position": 1, "level": 0})
    proxy_cmd("auto_conditioning_stop")
    return jsonify({"ok": True, "running": False, "status": "", "msg": "스마트 공조 중단됨"})

# ═══════════════════════════════════════════════════
# 애프터 블로우
# ═══════════════════════════════════════════════════
@app.route("/api/afterblow", methods=["POST"])
def api_afterblow():
    global _afterblow_stop, _afterblow_running
    try:
        check_pin()
    except PermissionError as e:
        return jsonify({"ok": False, "msg": str(e)}), 403

    if _afterblow_running:
        _afterblow_stop = True
        return jsonify({"ok": True, "running": False, "msg": "애프터 블로우 중단 중..."})

    def run():
        global _afterblow_stop, _afterblow_running
        _afterblow_running = True
        _afterblow_stop    = False
        lat, lon = 0, 0
        try:
            wake_vehicle()
            if _afterblow_stop: return
            data  = get_vehicle_data()
            drive = data.get("drive_state", {})
            lat   = drive.get("latitude", 0)
            lon   = drive.get("longitude", 0)
            if _afterblow_stop: return

            proxy_cmd("auto_conditioning_start")
            proxy_cmd("set_temps", {"driver_temp": 28.0, "passenger_temp": 28.0})
            time.sleep(2)
            
            # 열선 전부 끄기
            proxy_cmd("remote_auto_seat_climate_request", {"auto_seat_position": 1, "auto_climate_on": False})
            proxy_cmd("remote_auto_seat_climate_request", {"auto_seat_position": 2, "auto_climate_on": False})
            proxy_cmd("remote_seat_heater_request", {"seat_position": 0, "level": 0})
            proxy_cmd("remote_seat_heater_request", {"seat_position": 1, "level": 0})
            proxy_cmd("remote_steering_wheel_heater_request", {"on": False})

            send_naver_mail("애프터 블로우 시작", f"애프터 블로우가 시작되었습니다.\n{AFTERBLOW_SECONDS//60}분 후 자동 종료됩니다.")
            if lat and lon:
                proxy_cmd("window_control", {"command": "vent", "lat": lat, "lon": lon})
            for _ in range(AFTERBLOW_SECONDS):
                if _afterblow_stop: break
                time.sleep(1)
        finally:
            proxy_cmd("set_temps", {"driver_temp": DEFAULT_TEMP, "passenger_temp": DEFAULT_TEMP})
            if lat and lon:
                proxy_cmd("window_control", {"command": "close", "lat": lat, "lon": lon})
            proxy_cmd("auto_conditioning_stop")
            proxy_cmd("set_sentry_mode", {"on": False})
            send_naver_mail("애프터 블로우 종료", "애프터 블로우가 종료되었습니다.\n공조가 꺼지고 창문이 닫혔습니다.")
            _afterblow_running = False
            _afterblow_stop    = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "running": True, "msg": f"애프터 블로우 시작 ({AFTERBLOW_SECONDS//60}분) — 다시 누르면 중단"})

# ═══════════════════════════════════════════════════
# 공조 상태 읽기
# ═══════════════════════════════════════════════════
@app.route("/api/climate_status", methods=["POST"])
def api_climate_status():
    try:
        check_pin()
    except PermissionError as e:
        return jsonify({"ok": False, "msg": str(e)}), 403
    try:
        wake_vehicle()
        data = get_vehicle_data()
        climate = data.get("climate_state", {})
        return jsonify({
            "ok": True,
            "front_defroster": climate.get("is_front_defroster_on", False),
            "rear_defroster": climate.get("is_rear_defroster_on", False),
            "wiper_heater": climate.get("wiper_blade_heater", False),
            "steering_wheel_heater": climate.get("steering_wheel_heater", False),
        })
    except Exception as e:
        if "TOKEN_EXPIRED" in str(e):
            return jsonify({"ok": False, "msg": "토큰 만료", "need_reauth": True})
        return jsonify({"ok": False, "msg": str(e)})

# ═══════════════════════════════════════════════════
# 성에제거 토글
# ═══════════════════════════════════════════════════
@app.route("/api/defrost", methods=["POST"])
def api_defrost():
    try:
        check_pin()
    except PermissionError as e:
        return jsonify({"ok": False, "msg": str(e)}), 403
    try:
        data = request.json or {}
        on = data.get("on", False)
        wake_vehicle()
        if on:
            ok = proxy_cmd("set_preconditioning_max", {"on": True, "manual_override": True})
        else:
            ok = proxy_cmd("set_preconditioning_max", {"on": False, "manual_override": False})
            proxy_cmd("auto_conditioning_stop")
        return jsonify({"ok": ok, "on": on, "msg": "성에제거 " + ("켜짐" if on else "꺼짐")})
    except Exception as e:
        if "TOKEN_EXPIRED" in str(e):
            return jsonify({"ok": False, "msg": "토큰 만료", "need_reauth": True})
        return jsonify({"ok": False, "msg": str(e)})

# ═══════════════════════════════════════════════════
# 핸들 열선 토글
# ═══════════════════════════════════════════════════
@app.route("/api/steering_heat", methods=["POST"])
def api_steering_heat():
    try:
        check_pin()
    except PermissionError as e:
        return jsonify({"ok": False, "msg": str(e)}), 403
    try:
        data = request.json or {}
        on = data.get("on", False)
        wake_vehicle()
        ok = proxy_cmd("remote_steering_wheel_heater_request", {"on": on})
        return jsonify({"ok": ok, "on": on, "msg": "핸들 열선 " + ("켜짐" if on else "꺼짐")})
    except Exception as e:
        if "TOKEN_EXPIRED" in str(e):
            return jsonify({"ok": False, "msg": "토큰 만료", "need_reauth": True})
        return jsonify({"ok": False, "msg": str(e)})


# ═══════════════════════════════════════════════════
# 웹 UI
# ═══════════════════════════════════════════════════
@app.route("/")
def index():
    return Response(HTML_UI, mimetype="text/html")

HTML_UI = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<title>Tesla 제어판</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;}
body{background:#f5f5f7;color:#111;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh;}
#pin,#reauth{display:flex;flex-direction:column;align-items:center;padding:64px 32px 32px;min-height:100vh;}
#reauth{display:none;}
#main{display:none;padding-bottom:40px;min-height:100vh;}
.logo{margin-bottom:20px;}
h1{font-size:20px;font-weight:600;margin-bottom:6px;color:#111;}
.sub{color:#999;font-size:14px;margin-bottom:40px;}
.dots{display:flex;gap:18px;margin-bottom:44px;}
.dot{width:14px;height:14px;border-radius:50%;border:1.5px solid #d0d0d0;background:#fff;transition:all .12s;}
.dot.on{background:#e82127;border-color:#e82127;}
.pad{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;width:100%;max-width:300px;}
.k{background:#fff;border:0.5px solid #e0e0e0;border-radius:14px;padding:20px 0;font-size:22px;cursor:pointer;text-align:center;transition:background .1s;color:#111;box-shadow:0 1px 3px rgba(0,0,0,.06);}
.k:active{background:#f0f0f0;}
.k.sym{font-size:16px;color:#aaa;}
.k.blank{background:transparent;border-color:transparent;pointer-events:none;box-shadow:none;}
.perr{color:#e82127;font-size:13px;margin-top:14px;min-height:20px;text-align:center;}
.reauth-box{background:#fff;border-radius:16px;padding:24px;width:100%;max-width:340px;box-shadow:0 1px 4px rgba(0,0,0,.08);}
.reauth-title{font-size:16px;font-weight:600;margin-bottom:8px;color:#111;}
.reauth-desc{font-size:13px;color:#888;margin-bottom:20px;line-height:1.6;}
.reauth-btn{display:block;width:100%;background:#e82127;color:#fff;border:none;border-radius:12px;padding:14px;font-size:15px;font-weight:600;cursor:pointer;margin-bottom:12px;text-align:center;}
.reauth-btn:active{background:#c41e1e;}
.reauth-btn.sec{background:#fff;color:#111;border:0.5px solid #e0e0e0;}
.reauth-input{width:100%;background:#f5f5f7;border:0.5px solid #e0e0e0;border-radius:12px;padding:12px 14px;font-size:13px;color:#111;margin-bottom:12px;outline:none;}
.reauth-input:focus{border-color:#e82127;}
.reauth-msg{font-size:12px;text-align:center;min-height:18px;margin-top:4px;}
.reauth-msg.ok{color:#34c759;}
.reauth-msg.err{color:#e82127;}
.topbar{display:flex;align-items:center;justify-content:space-between;padding:22px 18px 14px;}
.carinfo{display:flex;align-items:center;gap:10px;}
.online{width:9px;height:9px;border-radius:50%;background:#34c759;}
.cn{font-size:16px;font-weight:600;color:#111;}
.cm{font-size:12px;color:#999;margin-top:2px;}
.lbtn{background:#fff;border:0.5px solid #e0e0e0;border-radius:9px;padding:7px 14px;color:#999;font-size:13px;cursor:pointer;}
.batt-card{background:#fff;border:0.5px solid #ebebeb;border-radius:16px;margin:0 14px 10px;padding:16px 18px;box-shadow:0 1px 4px rgba(0,0,0,.05);}
.batt-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;}
.batt-label{font-size:11px;color:#bbb;letter-spacing:.07em;text-transform:uppercase;}
.batt-meta{display:flex;align-items:center;gap:8px;}
.batt-state-dot{width:8px;height:8px;border-radius:50%;background:#bbb;margin-top:2px;}
.batt-state-dot.online{background:#34c759;}
.batt-state-dot.asleep{background:#f5a623;}
.batt-state-dot.offline{background:#e82127;}
.batt-refresh{font-size:12px;color:#e82127;cursor:pointer;padding:4px 10px;background:#fff0f0;border-radius:6px;}
.batt-row{display:flex;align-items:flex-end;gap:6px;margin-bottom:3px;}
.batt-pct{font-size:38px;font-weight:700;color:#111;line-height:1;}
.batt-unit{font-size:14px;color:#aaa;margin-bottom:5px;}
.batt-range{font-size:12px;color:#888;margin-bottom:10px;}
.batt-bar-wrap{background:#f0f0f0;border-radius:6px;height:7px;margin-bottom:12px;overflow:hidden;}
.batt-bar{height:7px;border-radius:6px;background:#34c759;transition:width .6s ease;}
.batt-bar.warn{background:#f5a623;}.batt-bar.bad{background:#e82127;}
.batt-divider{border:none;border-top:0.5px solid #f0f0f0;margin:0 0 12px;}
.batt-degrad-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;}
.batt-degrad-title{font-size:12px;color:#888;}
.batt-degrad-sub{font-size:11px;color:#bbb;margin-top:2px;}
.batt-degrad-val{font-size:22px;font-weight:700;color:#111;}
.batt-daily{font-size:11px;color:#bbb;text-align:right;margin-top:2px;}
.batt-proj-title{font-size:11px;color:#bbb;letter-spacing:.06em;text-transform:uppercase;margin-bottom:6px;}
.batt-proj-note{font-size:10px;color:#ccc;margin-bottom:8px;}
.batt-proj-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;}
.batt-proj-item{background:#f5f5f7;border-radius:10px;padding:10px 6px;text-align:center;}
.batt-proj-yr{font-size:11px;color:#aaa;margin-bottom:4px;}
.batt-proj-pct{font-size:15px;font-weight:700;color:#111;}
.batt-proj-range{font-size:10px;color:#bbb;margin-top:2px;}
.batt-loading{text-align:center;padding:24px;color:#bbb;font-size:13px;}
.batt-err{text-align:center;padding:24px;color:#e82127;font-size:13px;}
.batt-reauth{text-align:center;padding:16px;}
.batt-reauth-btn{display:inline-block;background:#fff0f0;color:#e82127;border:0.5px solid #f5c0c0;border-radius:10px;padding:10px 20px;font-size:13px;font-weight:600;cursor:pointer;margin-top:8px;}
.sec{color:#bbb;font-size:11px;letter-spacing:.07em;text-transform:uppercase;padding:6px 18px 10px;}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:11px;padding:0 14px 10px;}
.card{background:#fff;border:0.5px solid #ebebeb;border-radius:16px;padding:18px;cursor:pointer;position:relative;overflow:hidden;transition:background .1s,transform .08s;box-shadow:0 1px 4px rgba(0,0,0,.05);}
.card:active{background:#f7f7f7;transform:scale(.97);}
.card.wide{grid-column:1/-1;}
.card.wide .row{display:flex;align-items:center;gap:14px;}
.card.wide .row .ico{margin-bottom:0;flex-shrink:0;}
.ico{width:40px;height:40px;border-radius:12px;display:flex;align-items:center;justify-content:center;margin-bottom:12px;font-size:20px;}
.b{background:#f0f4ff;}.r{background:#fff0f0;}.g{background:#f0fff4;}.p{background:#f5f0ff;}
.cn2{font-size:14px;font-weight:600;color:#111;margin-bottom:4px;transition:color .2s;}
.cs{font-size:12px;color:#bbb;transition:color .2s;}
.mode-tag{display:inline-block;font-size:11px;font-weight:700;padding:3px 9px;border-radius:6px;margin-top:5px;}
.mode-tag.cool{background:#e0f0ff;color:#1a6abf;}
.mode-tag.heat{background:#fff0e0;color:#c46a00;}
.mode-tag.vent{background:#e0fff0;color:#1a8a50;}
.mode-tag.off{background:#ebebeb;color:#bbb;}
.result{font-size:11px;color:#666;margin-top:4px;min-height:14px;line-height:1.5;}
.card.on-blue{background:#f0f4ff;border:1.5px solid #4a90d9;}
.card.on-blue .cn2{color:#1a5fa8;}
.card.on-blue .cs{color:#4a90d9;}
.card.on-blue .ico.b{background:#d0e4ff;}
.card.on-red{background:#fff0f0;border:1.5px solid #e82127;}
.card.on-red .cn2{color:#c41e1e;}
.card.on-red .cs{color:#e82127;}
.card.on-red .ico.r{background:#ffd0d0;}
.state-badge{position:absolute;top:12px;right:12px;font-size:11px;font-weight:700;padding:3px 9px;border-radius:6px;}
.state-badge.off{background:#ebebeb;color:#bbb;}
.state-badge.on-b{background:#4a90d9;color:#fff;}
.state-badge.on-r{background:#e82127;color:#fff;}
.bar{position:absolute;bottom:0;left:0;height:2px;background:#e82127;width:0;transition:width .08s linear;}
#toast{position:fixed;bottom:28px;left:50%;transform:translateX(-50%);background:#333;color:#fff;font-size:13px;padding:11px 22px;border-radius:24px;opacity:0;transition:opacity .25s;pointer-events:none;max-width:88vw;text-align:center;}
#toast.show{opacity:1;}
.toggle-row{display:flex;gap:10px;padding:0 14px 10px;}
.toggle-btn{flex:1;background:#fff;border:0.5px solid #ebebeb;border-radius:12px;padding:14px 10px;display:flex;align-items:center;justify-content:center;gap:8px;cursor:pointer;transition:all .2s;box-shadow:0 1px 4px rgba(0,0,0,.05);}
.toggle-btn:active{transform:scale(.97);}
.toggle-btn .t-ico{font-size:18px;}
.toggle-btn .t-label{font-size:13px;font-weight:600;color:#999;}
.toggle-btn .t-dot{width:7px;height:7px;border-radius:50%;background:#ddd;transition:background .2s;}
.toggle-btn.on-defrost{background:#e8f4ff;border:1.5px solid #4a90d9;}
.toggle-btn.on-defrost .t-label{color:#1a5fa8;}
.toggle-btn.on-defrost .t-dot{background:#4a90d9;}
.toggle-btn.on-heat{background:#fff0f0;border:1.5px solid #e82127;}
.toggle-btn.on-heat .t-label{color:#c41e1e;}
.toggle-btn.on-heat .t-dot{background:#e82127;}
.toggle-btn .t-loading{width:14px;height:14px;border:2px solid #ddd;border-top:2px solid #999;border-radius:50%;animation:tspin .8s linear infinite;}
@keyframes tspin{to{transform:rotate(360deg);}}
</style>
</head>
<body>

<!-- PIN 화면 -->
<div id="pin">
  <div class="logo">
    <svg width="56" height="56" viewBox="0 0 1000 1000" xmlns="http://www.w3.org/2000/svg">
      <path fill="#e82127" d="M 963 134C 963 134 958 150 937 188C 783 120 632 99 500 100C 500 100 500 100 500 100C 368 99 217 120 63 188C 44 154 37 134 37 134C 206 67 364 44 500 44C 636 44 794 67 963 134C 963 134 963 134 963 134M 500 250C 500 250 595 134 595 134C 595 134 759 137 922 213C 880 275 798 306 798 306C 792 251 753 237 630 237C 630 237 500 966 500 966C 500 966 369 237 369 237C 247 237 208 251 202 306C 202 306 119 275 78 213C 241 137 404 134 404 134C 404 134 500 250 500 250"/>
    </svg>
  </div>
  <h1>Tesla 제어판</h1>
  <p class="sub">PIN을 입력하세요</p>
  <div class="dots">
    <div class="dot" id="d0"></div><div class="dot" id="d1"></div>
    <div class="dot" id="d2"></div><div class="dot" id="d3"></div>
  </div>
  <div class="pad">
    <button class="k" onclick="kp('1')">1</button><button class="k" onclick="kp('2')">2</button><button class="k" onclick="kp('3')">3</button>
    <button class="k" onclick="kp('4')">4</button><button class="k" onclick="kp('5')">5</button><button class="k" onclick="kp('6')">6</button>
    <button class="k" onclick="kp('7')">7</button><button class="k" onclick="kp('8')">8</button><button class="k" onclick="kp('9')">9</button>
    <div class="k blank"></div><button class="k" onclick="kp('0')">0</button><button class="k sym" onclick="kdel()">⌫</button>
  </div>
  <div class="perr" id="perr"></div>
</div>

<!-- 재인증 화면 -->
<div id="reauth">
  <div class="logo">
    <svg width="56" height="56" viewBox="0 0 1000 1000" xmlns="http://www.w3.org/2000/svg">
      <path fill="#e82127" d="M 963 134C 963 134 958 150 937 188C 783 120 632 99 500 100C 500 100 500 100 500 100C 368 99 217 120 63 188C 44 154 37 134 37 134C 206 67 364 44 500 44C 636 44 794 67 963 134C 963 134 963 134 963 134M 500 250C 500 250 595 134 595 134C 595 134 759 137 922 213C 880 275 798 306 798 306C 792 251 753 237 630 237C 630 237 500 966 500 966C 500 966 369 237 369 237C 247 237 208 251 202 306C 202 306 119 275 78 213C 241 137 404 134 404 134C 404 134 500 250 500 250"/>
    </svg>
  </div>
  <div class="reauth-box">
    <div class="reauth-title">Tesla 재인증 필요</div>
    <div class="reauth-desc">
      토큰이 만료되었습니다.<br>
      아래 버튼을 눌러 Tesla 로그인 후<br>
      리디렉션된 URL을 붙여넣으세요.
    </div>
    <button class="reauth-btn" onclick="openTeslaLogin()">Tesla 로그인 열기</button>
    <div style="font-size:12px;color:#bbb;margin-bottom:8px;">로그인 후 주소창 URL 전체 복사 → 아래 붙여넣기</div>
    <input class="reauth-input" id="reauth-url" placeholder="https://your-auth-server.example.com/callback?code=..." />
    <button class="reauth-btn" onclick="submitReauth()">인증 완료</button>
    <button class="reauth-btn sec" onclick="goPin()">PIN 화면으로</button>
    <div class="reauth-msg" id="reauth-msg"></div>
  </div>
</div>

<!-- 메인 화면 -->
<div id="main">
  <div class="topbar">
    <div class="carinfo">
    <svg width="24" height="24" viewBox="0 0 1000 1000" xmlns="http://www.w3.org/2000/svg">
      <path fill="#e82127" d="M 963 134C 963 134 958 150 937 188C 783 120 632 99 500 100C 500 100 500 100 500 100C 368 99 217 120 63 188C 44 154 37 134 37 134C 206 67 364 44 500 44C 636 44 794 67 963 134C 963 134 963 134 963 134M 500 250C 500 250 595 134 595 134C 595 134 759 137 922 213C 880 275 798 306 798 306C 792 251 753 237 630 237C 630 237 500 966 500 966C 500 966 369 237 369 237C 247 237 208 251 202 306C 202 306 119 275 78 213C 241 137 404 134 404 134C 404 134 500 250 500 250"/>
    </svg>
    <div>
      <div class="cn">Tesla 제어판</div>
      <div class="cm" style="display:flex;align-items:center;gap:5px;">
        <span style="width:7px;height:7px;border-radius:50%;background:#34c759;display:inline-block;"></span>
        My Tesla
      </div>
    </div>
</div>
    <button class="lbtn" onclick="goPin()">잠금</button>
  </div>

  <div class="batt-card">
    <div class="batt-top">
      <div class="batt-label">배터리 상태</div>
      <div class="batt-meta">
        <div class="batt-state-dot" id="vehicle-state"></div>
        <div class="batt-refresh" onclick="loadBattery()">새로고침</div>
      </div>
    </div>
    <div id="batt-content"><div class="batt-loading">배터리 정보 불러오는 중...</div></div>
  </div>
  <div class="toggle-row">
    <button class="toggle-btn" id="btn-defrost" onclick="toggleDefrost()">
      <span class="t-ico">❄️</span>
      <span class="t-label">성에제거</span>
      <span class="t-dot" id="dot-defrost"></span>
    </button>
    <button class="toggle-btn" id="btn-steering" onclick="toggleSteering()">
      <span class="t-ico">🔥</span>
      <span class="t-label">핸들 열선</span>
      <span class="t-dot" id="dot-steering"></span>
    </button>
  </div>
  <div class="sec">주요 기능</div>
  <div class="grid">

    <div class="card wide" id="c-climate_set" onclick="toggleCmd('climate_set')">
      <div class="state-badge off" id="badge-climate">OFF</div>
      <div class="row">
        <div class="ico b" id="ico-climate">🌡️</div>
        <div style="flex:1;">
          <div class="cn2" id="title-climate">스마트 공조</div>
          <div class="cs" id="desc-climate">터치하면 실행 — 다시 터치하면 중단</div>
          <div id="mode-tag-wrap"></div>
          <div class="result" id="r-climate_set"></div>
        </div>
      </div>
      <div class="bar" id="p-climate_set"></div>
    </div>
    <div class="card wide" id="c-afterblow" onclick="toggleCmd('afterblow')">
      <div class="state-badge off" id="badge-afterblow">OFF</div>
      <div class="row">
        <div class="ico r" id="ico-afterblow">💨</div>
        <div>
          <div class="cn2" id="title-afterblow">애프터 블로우</div>
          <div class="cs" id="desc-afterblow">터치하면 실행 — 다시 터치하면 중단</div>
          <div class="result" id="r-afterblow"></div>
        </div>
      </div>
      <div class="bar" id="p-afterblow"></div>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
let p='', storedPin='', busy=false;
function show(id){['pin','reauth','main'].forEach(s=>document.getElementById(s).style.display=s===id?(id==='main'?'block':'flex'):'none');}
function kp(d){if(p.length>=4)return;p+=d;updDots();if(p.length===4)setTimeout(checkPin,100);}
function kdel(){p=p.slice(0,-1);updDots();document.getElementById('perr').textContent='';}
function updDots(){for(let i=0;i<4;i++)document.getElementById('d'+i).classList.toggle('on',i<p.length);}
function checkPin(){
  fetch('/api/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:p})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){storedPin=p;show('main');loadBattery();loadClimateStatus();}
    else{
      document.getElementById('perr').textContent=d.msg||'잘못된 PIN입니다';
      for(let i=0;i<4;i++){const el=document.getElementById('d'+i);el.style.background='#e82127';el.style.borderColor='#e82127';}
      setTimeout(()=>{p='';updDots();for(let i=0;i<4;i++){const el=document.getElementById('d'+i);el.style.background='';el.style.borderColor='';}document.getElementById('perr').textContent='';},1200);
    }
  });
}
function goPin(){show('pin');p='';storedPin='';updDots();}

function handleReauth(){show('reauth');}
function openTeslaLogin(){
  fetch('/api/reauth_url',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:storedPin})})
  .then(r=>r.json()).then(d=>{
    if(d.ok) window.open(d.url,'_blank');
    else toast(d.msg||'오류');
  });
}
function submitReauth(){
  const url=document.getElementById('reauth-url').value.trim();
  const msg=document.getElementById('reauth-msg');
  if(!url){msg.textContent='URL을 입력하세요';msg.className='reauth-msg err';return;}
  msg.textContent='처리 중...';msg.className='reauth-msg';
  fetch('/api/reauth_code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:storedPin,callback_url:url})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){msg.textContent=d.msg;msg.className='reauth-msg ok';setTimeout(()=>{show('main');loadBattery();},1500);}
    else{msg.textContent=d.msg;msg.className='reauth-msg err';}
  });
}

function loadBattery(){
  const el=document.getElementById('batt-content');
  const stEl=document.getElementById('vehicle-state');
  el.innerHTML='<div class="batt-loading">배터리 정보 불러오는 중...</div>';
  fetch('/api/battery',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:storedPin})})
  .then(r=>r.json()).then(d=>{
    if(!d.ok){
      if(d.need_reauth){
        el.innerHTML='<div class="batt-reauth"><div style="color:#888;font-size:13px;">Tesla 토큰이 만료되었습니다</div><div class="batt-reauth-btn" onclick="handleReauth()">재인증 하기</div></div>';
      } else {
        el.innerHTML='<div class="batt-err">'+d.msg+'</div>';
      }
      return;
    }
    const st=d.vehicle_state||'unknown';
    stEl.textContent='';
    stEl.className='batt-state-dot '+({online:'online',asleep:'asleep'}[st]||'');
    const deg=d.current_degradation;
    const barColor=deg<5?'':deg<10?'warn':'bad';
    const proj=d.projections;
    const DELIVERY=d.delivery_range_km;
    el.innerHTML=`
      <div class="batt-row"><div class="batt-pct">${d.battery_level}</div><div class="batt-unit">%</div></div>
      <div class="batt-range">현재 ${d.range_km}km 주행 가능 &nbsp;|&nbsp; 80% 환산 ${d.range_at_80_km}km (기준 ${DELIVERY}km)</div>
      <div class="batt-bar-wrap"><div class="batt-bar ${barColor}" style="width:${Math.max(0,100-deg)}%"></div></div>
      <hr class="batt-divider">
      <div class="batt-degrad-row">
        <div><div class="batt-degrad-title">현재 배터리 열화율</div><div class="batt-degrad-sub">인도일로부터 ${d.days_since_delivery}일 경과</div></div>
        <div><div class="batt-degrad-val">${deg}%</div><div class="batt-daily">일일 ${d.daily_degradation}%</div></div>
      </div>
      <div class="batt-proj-title">향후 열화율 예측</div>
      <div class="batt-proj-note">※ 현재 실측값 기반 선형 추정 — 충전 습관에 따라 달라질 수 있음</div>
      <div class="batt-proj-grid">
        ${Object.entries(proj).map(([yr,v])=>`
        <div class="batt-proj-item">
          <div class="batt-proj-yr">${yr}년 후</div>
          <div class="batt-proj-pct">${v.degradation}%</div>
          <div class="batt-proj-range">${v.range_km}km</div>
        </div>`).join('')}
      </div>`;
  }).catch(()=>{el.innerHTML='<div class="batt-err">연결 오류 — 다시 시도하세요</div>';});
}

const cmdOn = {climate_set: false, afterblow: false};

function setModeTag(mode, status) {
  const wrap = document.getElementById('mode-tag-wrap');
  if (!status) { wrap.innerHTML = ''; return; }
  const cls  = {cool:'cool', heat:'heat', vent:'vent'}[mode] || 'off';
  const icon = {cool:'❄️', heat:'🔥', vent:'🌬️'}[mode] || '';
  wrap.innerHTML = `<span class="mode-tag ${cls}">${icon} ${status}</span>`;
}

function setCardOn(cmd, isOn, data) {
  cmdOn[cmd] = isOn;
  if (cmd === 'climate_set') {
    const card  = document.getElementById('c-climate_set');
    const badge = document.getElementById('badge-climate');
    const title = document.getElementById('title-climate');
    const desc  = document.getElementById('desc-climate');
    const res   = document.getElementById('r-climate_set');
    if (isOn) {
      card.classList.add('on-blue');
      badge.className = 'state-badge on-b'; badge.textContent = 'ON';
      title.textContent = '스마트 공조 작동 중';
      desc.textContent  = '다시 터치하면 중단';
      setModeTag(data.mode, data.status);
      if (res) res.textContent = data.msg || '';
    } else {
      card.classList.remove('on-blue');
      badge.className = 'state-badge off'; badge.textContent = 'OFF';
      title.textContent = '스마트 공조';
      desc.textContent  = '터치하면 실행 — 다시 터치하면 중단';
      setModeTag('', '');
      if (res) res.textContent = data.msg || '';
    }
  }
  if (cmd === 'afterblow') {
    const card  = document.getElementById('c-afterblow');
    const badge = document.getElementById('badge-afterblow');
    const title = document.getElementById('title-afterblow');
    const desc  = document.getElementById('desc-afterblow');
    if (isOn) {
      card.classList.add('on-red');
      badge.className = 'state-badge on-r'; badge.textContent = 'ON';
      title.textContent = '애프터 블로우 작동 중';
      desc.textContent  = '다시 터치하면 중단';
    } else {
      card.classList.remove('on-red');
      badge.className = 'state-badge off'; badge.textContent = 'OFF';
      title.textContent = '애프터 블로우';
      desc.textContent  = '터치하면 실행 — 다시 터치하면 중단';
    }
  }
}

function toggleCmd(cmd) {
  if (busy) { toast('처리 중입니다'); return; }
  busy = true;
  const bar = document.getElementById('p-' + cmd);
  const res = document.getElementById('r-' + cmd);
  if (res) res.textContent = '';
  let prog = 0;
  const iv = setInterval(() => { prog = Math.min(prog + 5, 90); bar.style.width = prog + '%'; }, 80);

  const apiUrl = (cmd === 'climate_set' && cmdOn[cmd]) ? '/api/climate_stop' : '/api/' + cmd;

  fetch(apiUrl, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({pin:storedPin})})
  .then(r => r.json()).then(data => {
    clearInterval(iv); bar.style.width = '100%';
    setTimeout(() => { bar.style.width = '0'; }, 400);
    if (data.need_reauth) { busy = false; handleReauth(); return; }
    if (data.ok) {
      setCardOn(cmd, data.running, data);
      toast(data.msg);
    } else {
      toast(data.msg || '오류 발생');
    }
    busy = false;
  }).catch(() => { clearInterval(iv); bar.style.width = '0'; toast('연결 오류'); busy = false; });
}

let climateState = {defrost: false, steering: false};

function loadClimateStatus(){
  const defBtn = document.getElementById('btn-defrost');
  const steBtn = document.getElementById('btn-steering');
  document.getElementById('dot-defrost').outerHTML='<span class="t-loading" id="dot-defrost"></span>';
  document.getElementById('dot-steering').outerHTML='<span class="t-loading" id="dot-steering"></span>';
  fetch('/api/climate_status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:storedPin})})
  .then(r=>r.json()).then(d=>{
    document.getElementById('dot-defrost').outerHTML='<span class="t-dot" id="dot-defrost"></span>';
    document.getElementById('dot-steering').outerHTML='<span class="t-dot" id="dot-steering"></span>';
    if(!d.ok){toast(d.msg||'상태 읽기 실패');return;}
    climateState.defrost = d.front_defroster || d.rear_defroster || d.wiper_heater;
    climateState.steering = d.steering_wheel_heater;
    defBtn.className = 'toggle-btn' + (climateState.defrost ? ' on-defrost' : '');
    steBtn.className = 'toggle-btn' + (climateState.steering ? ' on-heat' : '');
  }).catch(()=>{
    document.getElementById('dot-defrost').outerHTML='<span class="t-dot" id="dot-defrost"></span>';
    document.getElementById('dot-steering').outerHTML='<span class="t-dot" id="dot-steering"></span>';
  });
}

function toggleDefrost(){
  if(busy){toast('처리 중입니다');return;}
  busy=true;
  const on = !climateState.defrost;
  fetch('/api/defrost',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:storedPin,on:on})})
  .then(r=>r.json()).then(d=>{
    if(d.need_reauth){busy=false;handleReauth();return;}
    if(d.ok){
      climateState.defrost=d.on;
      document.getElementById('btn-defrost').className='toggle-btn'+(d.on?' on-defrost':'');
    }
    toast(d.msg);busy=false;
  }).catch(()=>{toast('연결 오류');busy=false;});
}

function toggleSteering(){
  if(busy){toast('처리 중입니다');return;}
  busy=true;
  const on = !climateState.steering;
  fetch('/api/steering_heat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:storedPin,on:on})})
  .then(r=>r.json()).then(d=>{
    if(d.need_reauth){busy=false;handleReauth();return;}
    if(d.ok){
      climateState.steering=d.on;
      document.getElementById('btn-steering').className='toggle-btn'+(d.on?' on-heat':'');
    }
    toast(d.msg);busy=false;
  }).catch(()=>{toast('연결 오류');busy=false;});
}

function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove('show'),3500);}
</script>
</body>
</html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
