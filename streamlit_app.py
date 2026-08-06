import threading

import av
import gspread
import numpy as np
import pandas as pd
import streamlit as st
import zxingcpp
from google.oauth2.service_account import Credentials
from streamlit_autorefresh import st_autorefresh
from streamlit_webrtc import (
    WebRtcMode,
    VideoProcessorBase,
    webrtc_streamer,
)


# =========================================================
# CONFIGURATION
# =========================================================

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


# =========================================================
# OUTILS
# =========================================================

def clean_barcode(value):
    """Nettoie un code-barres."""
    text = str(value or "")

    return (
        text
        .replace(" ", "")
        .replace("\n", "")
        .replace("\r", "")
        .strip()
    )


def barcode_digits(value):
    """Garde uniquement les chiffres."""
    value = clean_barcode(value)

    return "".join(
        character
        for character in value
        if character.isdigit()
    )


def barcode_variants(value):
    """
    Gère UPC-A et EAN-13.
    UPC-A peut apparaître avec un zéro initial
    sous forme EAN-13.
    """
    digits = barcode_digits(value)

    if not digits:
        return set()

    variants = {digits}

    if len(digits) == 12:
        variants.add("0" + digits)

    if len(digits) == 13 and digits.startswith("0"):
        variants.add(digits[1:])

    return variants


def safe_int(value):
    """Convertit une valeur en entier."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


# =========================================================
# DETECTION VIDEO EN DIRECT
# =========================================================

class BarcodeVideoProcessor(VideoProcessorBase):
    """
    Analyse les images de la caméra en continu.
    Le dernier code trouvé est conservé en mémoire.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.latest_code = None
        self.frame_number = 0

    def get_latest_code(self):
        with self.lock:
            return self.latest_code

    def recv(self, frame):
        self.frame_number += 1

        image = frame.to_ndarray(format="bgr24")

        # Analyse une image sur trois pour alléger le serveur.
        if self.frame_number % 3 == 0:
            try:
                results = zxingcpp.read_barcodes(
                    image,
                    try_rotate=True,
                    try_downscale=True,
                )

                for result in results:
                    detected_code = clean_barcode(
                        result.text
                    )

                    if detected_code:
                        with self.lock:
                            self.latest_code = (
                                detected_code
                            )
                        break

            except Exception:
                pass

        return av.VideoFrame.from_ndarray(
            image,
            format="bgr24",
        )


# =========================================================
# GOOGLE SHEETS
# =========================================================

@st.cache_resource
def get_client():
    """Crée la connexion Google Sheets."""
    try:
        service_account = st.secrets["gcp_service_account"]
    except KeyError as error:
        raise RuntimeError(
            "Le secret [gcp_service_account] est absent "
            "dans Streamlit Cloud."
        ) from error

    credentials = Credentials.from_service_account_info(
        dict(service_account),
        scopes=SCOPES,
    )

    return gspread.authorize(credentials)


def get_sheet(sheet_name):
    """Ouvre une feuille autorisée."""
    if sheet_name not in ALLOWED_SHEETS:
        raise ValueError(
            f"Feuille non autorisée : {sheet_name}"
        )

    client = get_client()
    spreadsheet = client.open_by_key(SHEET_ID)

    return spreadsheet.worksheet(sheet_name)


def get_header_map(worksheet):
    """Retourne les colonnes sous la forme nom -> numéro."""
    headers = worksheet.row_values(1)

    return {
        str(header).strip(): index + 1
        for index, header in enumerate(headers)
        if str(header).strip()
    }


def validate_headers(header_map):
    """Vérifie les colonnes nécessaires."""
    missing_headers = [
        header
        for header in REQUIRED_HEADERS
        if header not in header_map
    ]

    if missing_headers:
        raise ValueError(
            "Colonnes manquantes : "
            + ", ".join(missing_headers)
        )


def get_all_rows(sheet_name):
    """Lit les lignes de la feuille."""
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
    """Prépare le tableau affiché."""
    columns = [
        "Code barre",
        "JumiaSKU",
        "SellerSKU",
        "Quantity",
    ]

    rows = get_all_rows(sheet_name)

    if not rows:
        return pd.DataFrame(columns=columns)

    return pd.DataFrame(rows)[columns]


def scan_and_increment(sheet_name, barcode, quantity):
    """Recherche un code et augmente Quantity."""
    barcode = clean_barcode(barcode)
    barcode_options = barcode_variants(barcode)
    quantity = max(1, safe_int(quantity))

    if not barcode_options:
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
    quantity_column = header_map["Quantity"]

    barcode_index = barcode_column - 1
    jumia_index = header_map["JumiaSKU"] - 1
    seller_index = header_map["SellerSKU"] - 1

    for row_number, row in enumerate(values[1:], start=2):
        if len(row) <= barcode_index:
            continue

        sheet_barcode = clean_barcode(
            row[barcode_index]
        )

        sheet_options = barcode_variants(
            sheet_barcode
        )

        if not barcode_options.intersection(
            sheet_options
        ):
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
    """Efface toutes les valeurs de Quantity."""
    worksheet = get_sheet(sheet_name)
    header_map = get_header_map(worksheet)
    validate_headers(header_map)

    quantity_column = header_map["Quantity"]
    values = worksheet.get_all_values()

    if len(values) < 2:
        return

    last_row = len(values)

    first_cell = gspread.utils.rowcol_to_a1(
        2,
        quantity_column,
    )

    last_cell = gspread.utils.rowcol_to_a1(
        last_row,
        quantity_column,
    )

    worksheet.batch_clear(
        [f"{first_cell}:{last_cell}"]
    )


def display_scan_result(result):
    """Affiche le résultat du scan."""
    status = result.get("status")

    if status == "FOUND_INCREMENTED":
        st.success(
            f"✅ [{result['sheetName']}] "
            f"{result['productLabel']} | "
            f"+{result['addedQty']} | "
            f"Ancienne quantité : "
            f"{result['previousQty']} | "
            f"Nouvelle quantité : "
            f"{result['newQty']}"
        )

    elif status == "NOT_FOUND":
        st.error(
            f"❌ Code non trouvé dans "
            f"{result['sheetName']} : "
            f"{result['barcode']} | "
            f"Quantité demandée : "
            f"{result['addedQty']}"
        )

    else:
        st.error(
            result.get(
                "message",
                "Erreur inconnue.",
            )
        )


# =========================================================
# INTERFACE
# =========================================================

st.title("Scanner Fiche Réception")

st.caption(
    "Scanner caméra continu UPC/EAN relié à Google Sheets."
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

st_autorefresh(
    interval=1000,
    key="barcode_refresh",
)

st.subheader("Scanner en direct")

context = webrtc_streamer(
    key="barcode-live-scanner",
    mode=WebRtcMode.SENDRECV,
    video_processor_factory=BarcodeVideoProcessor,
    media_stream_constraints={
        "video": {
            "facingMode": "environment",
        },
        "audio": False,
    },
    rtc_configuration={
        "iceServers": [
            {
                "urls": [
                    "stun:stun.l.google.com:19302"
                ]
            }
        ]
    },
    async_processing=True,
)

detected_code = None

if context.video_processor:
    detected_code = (
        context.video_processor.get_latest_code()
    )

if detected_code:
    st.success(
        f"Code détecté en direct : {detected_code}"
    )

    if st.button(
        "Ajouter ce code à Google Sheets",
        type="primary",
    ):
        process_key = (
            f"{sheet_name}-"
            f"{detected_code}-"
            f"{quantity}"
        )

        previous_key = st.session_state.get(
            "last_process_key"
        )

        if process_key != previous_key:
            try:
                result = scan_and_increment(
                    sheet_name,
                    detected_code,
                    quantity,
                )

                st.session_state["last_process_key"] = (
                    process_key
                )

                display_scan_result(result)

            except Exception as error:
                st.error(
                    "Erreur Google Sheets : "
                    f"{error}"
                )
        else:
            st.info(
                "Ce code a déjà été ajouté avec "
                "cette quantité."
            )
else:
    st.info(
        "Démarre la caméra puis présente le "
        "code-barres devant l'objectif."
    )


# =========================================================
# VUE DE LA FEUILLE
# =========================================================

st.divider()
st.subheader("Vue de la feuille")

column_refresh, column_reset = st.columns(2)

with column_refresh:
    refresh_clicked = st.button(
        "Rafraîchir la feuille"
    )

with column_reset:
    reset_clicked = st.button(
        "Réinitialiser Quantity"
    )


if refresh_clicked:
    st.rerun()


if reset_clicked:
    try:
        reset_quantities(sheet_name)

        st.success(
            f"Quantity réinitialisé pour "
            f"{sheet_name}."
        )

        st.rerun()

    except Exception as error:
        st.error(
            "Erreur de réinitialisation : "
            f"{error}"
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
