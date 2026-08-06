import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials
from streamlit_qrcode_scanner import qrcode_scanner


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
    """Nettoie une valeur de code-barres."""
    return (
        str(value or "")
        .replace(" ", "")
        .replace("\n", "")
        .replace("\r", "")
        .strip()
    )


def barcode_digits(value):
    """Conserve uniquement les chiffres."""
    value = clean_barcode(value)

    return "".join(
        character
        for character in value
        if character.isdigit()
    )


def barcode_variants(value):
    """
    Gère les différences entre UPC-A et EAN-13.

    Exemple :
    UPC-A  : 123456789012
    EAN-13 : 0123456789012
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


def get_detected_text(value):
    """
    Convertit la réponse du scanner en texte.
    Le composant renvoie normalement une chaîne.
    """
    if value is None:
        return ""

    if isinstance(value, str):
        return clean_barcode(value)

    if isinstance(value, dict):
        for key in ["text", "value", "code", "data"]:
            if key in value:
                return clean_barcode(value[key])

    return clean_barcode(value)


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


# =========================================================
# LECTURE DE LA FEUILLE
# =========================================================

@st.cache_data(
    ttl=15,
    show_spinner=False,
)
def get_all_rows(sheet_name):
    """
    Lit les lignes Google Sheets.
    Le cache évite les erreurs de quota.
    """
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


@st.cache_data(
    ttl=15,
    show_spinner=False,
)
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
# MISE À JOUR GOOGLE SHEETS
# =========================================================

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

        get_all_rows.clear()
        get_fiche_df.clear()

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

    get_all_rows.clear()
    get_fiche_df.clear()


# =========================================================
# AFFICHAGE DES RÉSULTATS
# =========================================================

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
    "Scanner de codes-barres en direct relié à Google Sheets."
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

tab_scanner, tab_manual = st.tabs(
    [
        "Scanner caméra",
        "Saisie manuelle",
    ]
)


# =========================================================
# SCANNER CAMÉRA EN DIRECT
# =========================================================

with tab_scanner:
    st.write(
        "Autorisez la caméra puis placez le code-barres "
        "devant l'objectif."
    )

    detected_raw = qrcode_scanner(
        key="live_barcode_scanner"
    )

    detected_code = get_detected_text(
        detected_raw
    )

    if detected_code:
        st.success(
            f"Code détecté : {detected_code}"
        )

        scan_key = (
            f"{sheet_name}-"
            f"{detected_code}-"
            f"{quantity}"
        )

        if st.button(
            "Ajouter à Google Sheets",
            type="primary",
            key="add_detected_code",
        ):
            previous_scan_key = st.session_state.get(
                "last_scan_key"
            )

            if scan_key == previous_scan_key:
                st.info(
                    "Ce code a déjà été ajouté "
                    "avec cette quantité."
                )
            else:
                try:
                    result = scan_and_increment(
                        sheet_name,
                        detected_code,
                        quantity,
                    )

                    st.session_state["last_scan_key"] = (
                        scan_key
                    )

                    display_scan_result(result)

                except Exception as error:
                    st.error(
                        "Erreur Google Sheets : "
                        f"{error}"
                    )

    else:
        st.info(
            "Démarrez la caméra et présentez "
            "le code-barres devant l'objectif."
        )


# =========================================================
# SAISIE MANUELLE
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

        if not manual_barcode:
            st.warning(
                "Veuillez saisir un code-barres."
            )
        else:
            manual_key = (
                f"manual-"
                f"{sheet_name}-"
                f"{manual_barcode}-"
                f"{quantity}"
            )

            previous_manual_key = (
                st.session_state.get(
                    "last_manual_key"
                )
            )

            if manual_key == previous_manual_key:
                st.info(
                    "Ce code a déjà été ajouté "
                    "avec cette quantité."
                )
            else:
                try:
                    result = scan_and_increment(
                        sheet_name,
                        manual_barcode,
                        quantity,
                    )

                    st.session_state["last_manual_key"] = (
                        manual_key
                    )

                    display_scan_result(result)

                except Exception as error:
                    st.error(
                        "Erreur Google Sheets : "
                        f"{error}"
                    )


# =========================================================
# VUE DE LA FEUILLE
# =========================================================

st.divider()
st.subheader("Vue de la feuille")

column_refresh, column_reset = st.columns(2)

with column_refresh:
    refresh_clicked = st.button(
        "Rafraîchir",
        key="refresh_sheet",
    )

with column_reset:
    reset_clicked = st.button(
        "Réinitialiser Quantity",
        key="reset_quantity",
    )


if refresh_clicked:
    get_all_rows.clear()
    get_fiche_df.clear()
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
