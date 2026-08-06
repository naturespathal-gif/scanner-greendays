import streamlit as st
import cv2
import numpy as np
import gspread
from google.oauth2.service_account import Credentials
import pandas as pd

st.set_page_config(page_title="Scanner Fiche Réception", layout="centered")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

SHEET_ID = "1sD2xPso-dc-0KQsRpF-vP70W-xpWZE883oaUZLGnAeY"
ALLOWED_SHEETS = ["eligible green", "eligible natural"]
REQUIRED_HEADERS = ["JumiaSKU", "SellerSKU", "Quantity", "Code barre"]

@st.cache_resource
def get_client():
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=SCOPES
    )
    return gspread.authorize(creds)

def get_sheet(sheet_name):
    client = get_client()
    ss = client.open_by_key(SHEET_ID)
    return ss.worksheet(sheet_name)

def get_header_map(ws):
    headers = ws.row_values(1)
    return {str(h).strip(): i + 1 for i, h in enumerate(headers)}

def validate_headers(header_map):
    missing = [h for h in REQUIRED_HEADERS if h not in header_map]
    if missing:
        raise ValueError("Colonnes manquantes: " + ", ".join(missing))

def clean_barcode(value):
    return str(value or "").replace(" ", "").strip()

def safe_int(value):
    try:
        return int(str(value).strip())
    except:
        return 0

def get_all_rows(sheet_name):
    ws = get_sheet(sheet_name)
    header_map = get_header_map(ws)
    validate_headers(header_map)

    data = ws.get_all_values()
    if len(data) < 2:
        return []

    barcode_col = header_map["Code barre"] - 1
    jumia_col = header_map["JumiaSKU"] - 1
    seller_col = header_map["SellerSKU"] - 1
    qty_col = header_map["Quantity"] - 1

    rows = []
    for idx, row in enumerate(data[1:], start=2):
        max_idx = max(barcode_col, jumia_col, seller_col, qty_col)
        if len(row) <= max_idx:
            continue

        rows.append({
            "row_number": idx,
            "Code barre": row[barcode_col],
            "JumiaSKU": row[jumia_col],
            "SellerSKU": row[seller_col],
            "Quantity": safe_int(row[qty_col]),
        })
    return rows

def get_fiche_df(sheet_name):
    rows = get_all_rows(sheet_name)
    if not rows:
        return pd.DataFrame(columns=["Code barre", "JumiaSKU", "SellerSKU", "Quantity"])
    return pd.DataFrame(rows)[["Code barre", "JumiaSKU", "SellerSKU", "Quantity"]]

def scan_and_increment(sheet_name, barcode, qty):
    barcode = clean_barcode(barcode)
    qty = max(1, int(qty))

    ws = get_sheet(sheet_name)
    header_map = get_header_map(ws)
    validate_headers(header_map)

    rows = ws.get_all_values()
    if len(rows) < 2:
        return {"status": "ERROR", "message": "La feuille est vide."}

    barcode_col = header_map["Code barre"] - 1
    jumia_col = header_map["JumiaSKU"] - 1
    seller_col = header_map["SellerSKU"] - 1
    qty_col = header_map["Quantity"]

    for i, row in enumerate(rows[1:], start=2):
        if len(row) <= barcode_col:
            continue

        row_barcode = clean_barcode(row[barcode_col])
        if row_barcode == barcode:
            current_qty = safe_int(row[qty_col - 1] if len(row) >= qty_col else 0)
            new_qty = current_qty + qty
            ws.update_cell(i, qty_col, new_qty)

            jumia = row[jumia_col] if len(row) > jumia_col else ""
            seller = row[seller_col] if len(row) > seller_col else ""

            return {
                "status": "FOUND_INCREMENTED",
                "sheetName": sheet_name,
                "barcode": barcode,
                "jumiaSku": jumia,
                "sellerSku": seller,
                "addedQty": qty,
                "previousQty": current_qty,
                "newQty": new_qty,
                "productLabel": seller or jumia or barcode
            }

    return {
        "status": "NOT_FOUND",
        "sheetName": sheet_name,
        "barcode": barcode,
        "addedQty": qty
    }

def reset_quantities(sheet_name):
    ws = get_sheet(sheet_name)
    header_map = get_header_map(ws)
    validate_headers(header_map)

    qty_col = header_map["Quantity"]
    rows = ws.get_all_values()
    last_row = len(rows)

    if last_row < 2:
        return

    for r in range(2, last_row + 1):
        ws.update_cell(r, qty_col, "")

def decode_barcode_from_image_bytes(image_bytes):
    file_bytes = np.frombuffer(image_bytes, np.uint8)
    image = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)

   detector = cv2.QRCodeDetector()

decoded_info, points, straight_qrcode = detector.detectAndDecode(image)

if decoded_info:
    return decoded_info

return None

st.title("Scanner Fiche Réception")
st.caption("Application Python + Streamlit reliée à Google Sheets.")

sheet_name = st.selectbox("Choisissez la feuille", ALLOWED_SHEETS)
qty = st.number_input("Quantité à ajouter", min_value=1, step=1, value=1)

tab1, tab2 = st.tabs(["Scanner caméra", "Saisie manuelle"])

barcode_to_process = None

with tab1:
    st.write("Prenez une photo du code-barres.")
    camera_image = st.camera_input("Caméra")

    if camera_image is not None:
        decoded = decode_barcode_from_image_bytes(camera_image.getvalue())
        if decoded:
            st.success(f"Code détecté : {decoded}")
            barcode_to_process = decoded
        else:
            st.warning("Aucun code-barres détecté. Essayez une photo plus nette ou utilisez la saisie manuelle.")

with tab2:
    manual_barcode = st.text_input("Code-barres manuel")
    if st.button("Valider le code manuel"):
        if manual_barcode.strip():
            barcode_to_process = manual_barcode.strip()

if barcode_to_process:
    try:
        result = scan_and_increment(sheet_name, barcode_to_process, qty)
        if result["status"] == "FOUND_INCREMENTED":
            st.success(
                f"✅ [{result['sheetName']}] {result['productLabel']} | "
                f"+{result['addedQty']} | Ancien: {result['previousQty']} | Nouveau: {result['newQty']}"
            )
        elif result["status"] == "NOT_FOUND":
            st.error(
                f"❌ Non trouvé dans {result['sheetName']} : {result['barcode']} | "
                f"Qté demandée: {result['addedQty']}"
            )
        else:
            st.error(result.get("message", "Erreur inconnue"))
    except Exception as e:
        st.error(f"Erreur: {e}")

st.divider()
st.subheader("Vue de la feuille")

col1, col2 = st.columns(2)

with col1:
    if st.button("Rafraîchir"):
        st.rerun()

with col2:
    if st.button("Réinitialiser Quantity"):
        try:
            reset_quantities(sheet_name)
            st.success(f"Quantity réinitialisé pour {sheet_name}")
            st.rerun()
        except Exception as e:
            st.error(f"Erreur reset: {e}")

try:
    df = get_fiche_df(sheet_name)
    st.dataframe(df, use_container_width=True)
except Exception as e:
    st.error(f"Erreur lecture feuille: {e}")
