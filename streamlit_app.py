import hashlib

import cv2
import gspread
import numpy as np
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials


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
# FONCTIONS UTILITAIRES
# =========================================================

def clean_barcode(value):
    """Nettoie un code-barres."""
    return (
        str(value or "")
        .replace(" ", "")
        .replace("\n", "")
        .replace("\r", "")
        .strip()
    )


def safe_int(value):
    """Convertit une valeur en entier."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def extract_decoded_value(decoded_values):
    """Extrait le premier code détecté."""
    if decoded_values is None:
        return None

    if isinstance(decoded_values, (list, tuple)):
        for value in decoded_values:
            cleaned = clean_barcode(value)

            if cleaned:
                return cleaned

        return None

    try:
        if hasattr(decoded_values, "tolist"):
            values = decoded_values.tolist()

            if isinstance(values, list):
                for value in values:
                    cleaned = clean_barcode(value)

                    if cleaned:
                        return cleaned

                return None
    except Exception:
        pass

    cleaned = clean_barcode(decoded_values)

    if cleaned:
        return cleaned

    return None


# =========================================================
# CONNEXION GOOGLE SHEETS
# =========================================================

@st.cache_resource
def get_client():
    """Crée la connexion au compte de service Google."""
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
            "Colonnes manquantes dans la feuille : "
            + ", ".join(missing_headers)
        )


# =========================================================
# DETECTION DU CODE-BARRES
# =========================================================

def get_barcode_detector():
    """Retourne le BarcodeDetector OpenCV disponible."""
    if hasattr(cv2, "barcode"):
        if hasattr(
            cv2.barcode,
            "BarcodeDetector",
        ):
            return cv2.barcode.BarcodeDetector()

    if hasattr(cv2, "barcode_BarcodeDetector"):
        return cv2.barcode_BarcodeDetector()

    raise RuntimeError(
        "BarcodeDetector est absent. "
        "Utilisez opencv-contrib-python-headless "
        "dans requirements.txt."
    )


def decode_barcode_from_image_bytes(image_bytes):
    """
    Détecte un code-barres UPC ou EAN.
    Retourne le code trouvé ou None.
    """
    if not image_bytes:
        return None

    file_array = np.frombuffer(
        image_bytes,
        dtype=np.uint8,
    )

    original_image = cv2.imdecode(
        file_array,
        cv2.IMREAD_COLOR,
    )

    if original_image is None:
        return None

    detector = get_barcode_detector()

    images_to_test = [
        original_image,
    ]

    height, width = original_image.shape[:2]

    if width < 1600:
        enlarged_image = cv2.resize(
            original_image,
            None,
            fx=2,
            fy=2,
            interpolation=cv2.INTER_CUBIC,
        )

        images_to_test.append(enlarged_image)

    gray_image = cv2.cvtColor(
        original_image,
        cv2.COLOR_BGR2GRAY,
    )

    images_to_test.append(gray_image)

    contrast_image = cv2.convertScaleAbs(
        gray_image,
        alpha=1.5,
        beta=0,
    )

    images_to_test.append(contrast_image)

    for image in images_to_test:
        try:
            if hasattr(
                detector,
                "detectAndDecodeWithType",
            ):
                result = detector.detectAndDecodeWithType(
                    image
                )
            else:
                result = detector.detectAndDecode(
                    image
                )

            if not isinstance(result, tuple):
                continue

            if len(result) < 2:
                continue

            detected = result[0]
            decoded_values = result[1]

            try:
                detected_ok = bool(detected)
            except Exception:
                detected_ok = False

            if not detected_ok:
                continue

            decoded_value = extract_decoded_value(
                decoded_values
            )

            if decoded_value:
                return decoded_value

        except Exception:
            continue

    return None


# =========================================================
# LECTURE GOOGLE SHEETS
# =========================================================

def get_all_rows(sheet_name):
    """Récupère les lignes de la feuille."""
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


# =========================================================
# MISE A JOUR GOOGLE SHEETS
# =========================================================

def scan_and_increment(sheet_name, barcode, quantity):
    """Recherche le code et augmente Quantity."""
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
    quantity_column = header_map["Quantity"]

    barcode_index = barcode_column - 1
    jumia_index = header_map["JumiaSKU"] - 1
    seller_index = header_map["SellerSKU"] - 1

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
    """Efface la colonne Quantity."""
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


# =========================================================
# AFFICHAGE DES RESULTATS
# =========================================================

def display_scan_result(result):
    """Affiche le résultat du traitement."""
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


# =========================================================
# ONGLET CAMERA
# =========================================================

with tab_camera:
    st.write(
        "Photographiez uniquement le code-barres "
        "UPC/EAN du produit iHerb."
    )

    camera_image = st.camera_input("Caméra")

    if camera_image is not None:
        image_bytes = camera_image.getvalue()

        image_hash = hashlib.sha256(
            image_bytes
        ).hexdigest()

        try:
            decoded = decode_barcode_from_image_bytes(
                image_bytes
            )

            if decoded:
                st.success(
                    f"Code détecté : {decoded}"
                )

                barcode_to_process = decoded
                process_key = f"camera-{image_hash}-{sheet_name}-{quantity}"

            else:
                st.warning(
                    "Aucun code-barres détecté. "
                    "Photographiez le code de face, "
                    "sans reflet et en vous rapprochant."
                )

        except Exception as error:
            st.error(
                f"Erreur scanner : {error}"
            )


# =========================================================
# ONGLET SAISIE MANUELLE
# =========================================================

with tab_manual:
    with st.form("manual_barcode_form"):
        manual_barcode = st.text_input(
            "Code-barres manuel"
        )

        manual_submitted = st.form_submit_button(
            "Valider le code manuel"
        )

    if manual_submitted:
        manual_barcode = clean_barcode(
            manual_barcode
        )

        if manual_barcode:
            barcode_to_process = manual_barcode
            process_key = f"manual-{manual_barcode}-{sheet_name}-{quantity}"

        else:
            st.warning(
                "Veuillez saisir un code-barres."
            )


# =========================================================
# TRAITEMENT DU CODE
# =========================================================

if barcode_to_process and process_key:
    previous_process_key = st.session_state.get(
        "last_process_key"
    )

    if process_key != previous_process_key:
        try:
            result = scan_and_increment(
                sheet_name,
                barcode_to_process,
                quantity,
            )

            st.session_state["last_process_key"] = (
                process_key
            )

            display_scan_result(result)

        except Exception as error:
            st.error(
                "Erreur pendant la mise à jour "
                f"de Google Sheets : {error}"
            )


# =========================================================
# VUE DE LA FEUILLE
# =========================================================

st.divider()
st.subheader("Vue de la feuille")

column_refresh, column_reset = st.columns(2)

with column_refresh:
    refresh_clicked = st.button(
        "Rafraîchir"
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
            "Erreur pendant la réinitialisation : "
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
