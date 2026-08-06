import hashlib

import cv2
import gspread
import numpy as np
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials


st.set_page_config(
    page_title="Scanner Fiche Réception",
    layout="centered",
)


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_ID = "1sD2xPso-dc-0KQsRpF-vP70W-xpWZE883oaUZLGnAeY"

ALLOWED_SHEETS = [
    "eligible green",
    "eligible natural",
]

REQUIRED_HEADERS = [
    "JumiaSKU",
    "SellerSKU",
    "Quantity",
    "Code barre",
]


def clean_barcode(value):
    """Nettoie un code-barres ou un QR code."""
    return str(value or "").replace(" ", "").replace("\n", "").strip()


def safe_int(value):
    """Convertit une valeur en entier sans faire planter l'application."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


@st.cache_resource
def get_client():
    """Crée et conserve la connexion Google Sheets."""
    try:
        service_account = st.secrets["gcp_service_account"]
    except KeyError as error:
        raise RuntimeError(
            "Le secret [gcp_service_account] est absent dans Streamlit Cloud."
        ) from error

    credentials = Credentials.from_service_account_info(
        dict(service_account),
        scopes=SCOPES,
    )

    return gspread.authorize(credentials)


def get_sheet(sheet_name):
    """Ouvre une feuille Google Sheets."""
    if sheet_name not in ALLOWED_SHEETS:
        raise ValueError(f"Feuille non autorisée : {sheet_name}")

    client = get_client()
    spreadsheet = client.open_by_key(SHEET_ID)

    return spreadsheet.worksheet(sheet_name)


def get_header_map(worksheet):
    """Retourne un dictionnaire nom_colonne -> numéro de colonne."""
    headers = worksheet.row_values(1)

    return {
        str(header).strip(): index + 1
        for index, header in enumerate(headers)
        if str(header).strip()
    }


def validate_headers(header_map):
    """Vérifie la présence des colonnes nécessaires."""
    missing_headers = [
        header
        for header in REQUIRED_HEADERS
        if header not in header_map
    ]

    if missing_headers:
        raise ValueError(
            "Colonnes manquantes dans la feuille : "
            + ", ".join(missing_headers)
        )


def decode_barcode_from_image_bytes(image_bytes):
    """
    Décode d'abord un QR code.
    Si aucun QR code n'est détecté, tente un code-barres classique.
    """
    if not image_bytes:
        return None

    file_array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(file_array, cv2.IMREAD_COLOR)

    if image is None:
        return None

    # Tentative QR code.
    qr_detector = cv2.QRCodeDetector()

    try:
        decoded_info, points, straight_qrcode = (
            qr_detector.detectAndDecode(image)
        )

        decoded_info = clean_barcode(decoded_info)

        if decoded_info:
            return decoded_info
    except cv2.error:
        pass

    # Tentative code-barres classique si disponible.
    try:
        if hasattr(cv2, "barcode") and hasattr(
            cv2.barcode,
            "BarcodeDetector",
        ):
            barcode_detector = cv2.barcode.BarcodeDetector()
            result = barcode_detector.detectAndDecode(image)

            if isinstance(result, tuple):
                if len(result) == 4:
                    ok, decoded_info, decoded_type, points = result

                    if ok and decoded_info:
                        if isinstance(decoded_info, (list, tuple)):
                            return clean_barcode(decoded_info[0])

                        return clean_barcode(decoded_info)

                elif len(result) == 3:
                    decoded_info, decoded_type, points = result

                    if isinstance(decoded_info, (list, tuple)):
                        if decoded_info:
                            return clean_barcode(decoded_info[0])

                    elif decoded_info:
                        return clean_barcode(decoded_info)

    except (cv2.error, AttributeError, TypeError, ValueError):
        pass

    return None


def get_all_rows(sheet_name):
    """Récupère les lignes utiles d'une feuille."""
    worksheet = get_sheet(sheet_name)
    header_map = get_header_map(worksheet)
    validate_headers(header_map)

    values = worksheet.get_all_values()

    if len(values) < 2:
        return []

    barcode_index = header_map["Code barre"] - 1
    jumia_index = header_map["JumiaSKU"] - 1
    seller_index = header_map["SellerSKU"] - 1
    quantity_index = header_map["Quantity"] - 1

    maximum_index = max(
        barcode_index,
        jumia_index,
        seller_index,
        quantity_index,
    )

    rows = []

    for row_number, row in enumerate(values[1:], start=2):
        if len(row) <= maximum_index:
            continue

        rows.append(
            {
                "row_number": row_number,
                "Code barre": row[barcode_index],
                "JumiaSKU": row[jumia_index],
                "SellerSKU": row[seller_index],
                "Quantity": safe_int(row[quantity_index]),
            }
        )

    return rows


def get_fiche_df(sheet_name):
    """Construit le tableau affiché dans Streamlit."""
    rows = get_all_rows(sheet_name)

    columns = [
        "Code barre",
        "JumiaSKU",
        "SellerSKU",
        "Quantity",
    ]

    if not rows:
        return pd.DataFrame(columns=columns)

    return pd.DataFrame(rows)[columns]


def scan_and_increment(sheet_name, barcode, quantity):
    """Recherche un code et augmente sa quantité."""
    barcode = clean_barcode(barcode)
    quantity = max(1, safe_int(quantity))

    if not barcode:
        return {
            "status": "ERROR",
            "message": "Le code-barres est vide.",
        }

    worksheet = get_sheet(sheet_name)
    header_map = get_header_map(worksheet)
    validate_headers(header_map)

    values = worksheet.get_all_values()

    if len(values) < 2:
        return {
            "status": "ERROR",
            "message": "La feuille est vide.",
        }

    barcode_column = header_map["Code barre"]
    jumia_index = header_map["JumiaSKU"] - 1
    seller_index = header_map["SellerSKU"] - 1

    barcode_index = barcode_column - 1
    quantity_column = header_map["Quantity"]

    for row_number, row in enumerate(values[1:], start=2):
        if len(row) <= barcode_index:
            continue

        row_barcode = clean_barcode(row[barcode_index])

        if row_barcode != barcode:
            continue

        current_quantity = 0

        if len(row) >= quantity_column:
            current_quantity = safe_int(
                row[quantity_column - 1]
            )

        new_quantity = current_quantity + quantity

        worksheet.update_cell(
            row_number,
            quantity_column,
            new_quantity,
        )

        jumia_sku = ""
        seller_sku = ""

        if len(row) > jumia_index:
            jumia_sku = str(row[jumia_index]).strip()

        if len(row) > seller_index:
            seller_sku = str(row[seller_index]).strip()

        product_label = seller_sku or jumia_sku or barcode

        return {
            "status": "FOUND_INCREMENTED",
            "sheetName": sheet_name,
            "barcode": barcode,
            "jumiaSku": jumia_sku,
            "sellerSku": seller_sku,
            "addedQty": quantity,
            "previousQty": current_quantity,
            "newQty": new_quantity,
            "productLabel": product_label,
        }

    return {
        "status": "NOT_FOUND",
        "sheetName": sheet_name,
        "barcode": barcode,
        "addedQty": quantity,
    }


def reset_quantities(sheet_name):
    """Vide la colonne Quantity sans toucher aux autres colonnes."""
    worksheet = get_sheet(sheet_name)
    header_map = get_header_map(worksheet)
    validate_headers(header_map)

    quantity_column = header_map["Quantity"]
    values = worksheet.get_all_values()

    if len(values) < 2:
        return

    last_row = len(values)

    worksheet.batch_clear(
        [
            f"{gspread.utils.rowcol_to_a1(2, quantity_column)}:"
            f"{gspread.utils.rowcol_to_a1(last_row, quantity_column)}"
        ]
    )


def display_scan_result(result):
    """Affiche le résultat du scan."""
    status = result.get("status")

    if status == "FOUND_INCREMENTED":
        st.success(
            f"✅ [{result['sheetName']}] "
            f"{result['productLabel']} | "
            f"+{result['addedQty']} | "
            f"Ancienne quantité : {result['previousQty']} | "
            f"Nouvelle quantité : {result['newQty']}"
        )

    elif status == "NOT_FOUND":
        st.error(
            f"❌ Code non trouvé dans {result['sheetName']} : "
            f"{result['barcode']} | "
            f"Quantité demandée : {result['addedQty']}"
        )

    else:
        st.error(
            result.get(
                "message",
                "Erreur inconnue.",
            )
        )


st.title("Scanner Fiche Réception")
st.caption(
    "Application Streamlit reliée à Google Sheets."
)

sheet_name = st.selectbox(
    "Choisissez la feuille",
    ALLOWED_SHEETS,
)

quantity = st.number_input(
    "Quantité à ajouter",
    min_value=1,
    step=1,
    value=1,
)

tab_camera, tab_manual = st.tabs(
    [
        "Scanner caméra",
        "Saisie manuelle",
    ]
)

barcode_to_process = None
process_key = None

with tab_camera:
    st.write(
        "Prenez une photo nette du QR code ou du code-barres."
    )

    camera_image = st.camera_input("Caméra")

    if camera_image is not None:
        image_bytes = camera_image.getvalue()
        image_hash = hashlib.sha256(image_bytes).hexdigest()

        barcode_to_process = decode_barcode_from_image_bytes(
            image_bytes
        )

        process_key = f"camera-{image_hash}-{sheet_name}-{quantity}"

        if barcode_to_process:
            st.success(
                f"Code détecté : {barcode_to_process}"
            )
        else:
            st.warning(
                "Aucun QR code ou code-barres détecté. "
                "Essayez une photo plus nette ou utilisez la saisie manuelle."
            )

with tab_manual:
    with st.form("manual_barcode_form"):
        manual_barcode = st.text_input(
            "Code-barres manuel"
        )

        manual_submitted = st.form_submit_button(
            "Valider le code manuel"
        )

    if manual_submitted:
        manual_barcode = clean_barcode(manual_barcode)

        if manual_barcode:
            barcode_to_process = manual_barcode
            process_key = (
                f"manual-{manual_barcode}-{sheet_name}-{quantity}"
            )
        else:
            st.warning(
                "Veuillez saisir un code-barres."
            )


if barcode_to_process and process_key:
    last_process_key = st.session_state.get(
        "last_process_key"
    )

    if process_key != last_process_key:
        try:
            result = scan_and_increment(
                sheet_name,
                barcode_to_process,
                quantity,
            )

            st.session_state["last_process_key"] = process_key
            display_scan_result(result)

        except Exception as error:
            st.error(
                f"Erreur pendant la mise à jour de Google Sheets : {error}"
            )


st.divider()
st.subheader("Vue de la feuille")

column_refresh, column_reset = st.columns(2)

with column_refresh:
    refresh_clicked = st.button("Rafraîchir")

with column_reset:
    reset_clicked = st.button("Réinitialiser Quantity")


if refresh_clicked:
    st.rerun()


if reset_clicked:
    try:
        reset_quantities(sheet_name)
        st.success(
            f"Quantity réinitialisé pour {sheet_name}."
        )
        st.rerun()

    except Exception as error:
        st.error(
            f"Erreur pendant la réinitialisation : {error}"
        )


try:
    fiche_df = get_fiche_df(sheet_name)

    st.dataframe(
        fiche_df,
        use_container_width=True,
        hide_index=True,
    )

except Exception as error:
    st.error(
        f"Erreur lecture feuille : {error}"
    )
