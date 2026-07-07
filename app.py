import streamlit as st
import pandas as pd
import re

GARAGE_ORDER = ["G1", "G2", "G5", "G6", "G7", "SS"]


def normalize_garage(name: str) -> str:
    name = str(name).strip()
    # "G7 BRT" → "G7"
    name = re.sub(r"\s+BRT$", "", name, flags=re.IGNORECASE)
    return name


def _to_csv_url(url: str) -> str:
    """Normaliza um link 'Publicar na web' do Google Sheets para saída CSV."""
    url = url.strip()
    if "output=csv" in url:
        return url
    if "/pubhtml" in url:
        url = url.replace("/pubhtml", "/pub")
    if "/pub" in url:
        return url + ("&" if "?" in url else "?") + "output=csv"
    return url


@st.cache_data(ttl=300, show_spinner=False)
def load_saldo_raw() -> pd.DataFrame:
    """Lê a aba publicada da planilha Google (CSV) de st.secrets['saldo_unid_url']."""
    try:
        url = st.secrets["saldo_unid_url"]
    except (KeyError, FileNotFoundError):
        raise RuntimeError(
            "Secret 'saldo_unid_url' não encontrado. Defina a URL da planilha "
            "Google publicada nos secrets do Streamlit."
        )
    return pd.read_csv(_to_csv_url(url), header=None, dtype=str)


def parse_saldo(df_raw: pd.DataFrame) -> pd.DataFrame:
    rows = []
    current_garage = None

    for _, row in df_raw.iterrows():
        row_vals = [str(v).strip() if pd.notna(v) else "" for v in row]
        row_text = " ".join(v for v in row_vals if v)

        if "Deposito" in row_text or "Depósito" in row_text:
            # Detect garage header: "Deposito : G1 G1"
            match = re.search(r"Dep[oó]sito\s*:\s*(\S+(?:\s+BRT)?)", row_text, re.IGNORECASE)
            if match:
                current_garage = normalize_garage(match.group(1))
            continue

        if current_garage is None:
            continue

        # Data rows: first column must be a numeric product code
        col0 = row_vals[0]
        if not re.match(r"^\d{5,}$", col0):
            continue

        product_code = int(col0)
        description = row_vals[2] if len(row_vals) > 2 else ""

        # Quantity in stock is column index 10
        try:
            qty_raw = row_vals[10] if len(row_vals) > 10 else ""
            qty = float(qty_raw.replace(",", ".")) if qty_raw else 0.0
        except ValueError:
            qty = 0.0

        rows.append({
            "garagem": current_garage,
            "codigo": product_code,
            "descricao": description,
            "saldo": qty,
        })

    return pd.DataFrame(rows)


@st.cache_data(ttl=300, show_spinner=False)
def load_maxmin_raw() -> pd.DataFrame:
    """Lê a aba publicada de máx/mín (CSV wide) de st.secrets['max_min_url']."""
    try:
        url = st.secrets["max_min_url"]
    except (KeyError, FileNotFoundError):
        raise RuntimeError(
            "Secret 'max_min_url' não encontrado. Defina a URL da planilha "
            "Google publicada nos secrets do Streamlit."
        )
    return pd.read_csv(_to_csv_url(url), dtype=str)


def parse_maxmin(df: pd.DataFrame) -> pd.DataFrame:
    """Formato wide: 1ª coluna = código; depois metadados e um bloco de colunas
    por garagem com o estoque MÁXIMO. Usa a primeira ocorrência de cada garagem
    (bloco máximo) e ignora o bloco de mínimo — o cálculo só usa o máximo."""
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    codigo = pd.to_numeric(df.iloc[:, 0], errors="coerce")

    # Primeira ocorrência de cada garagem de interesse = bloco do MÁXIMO
    col_por_garagem = {}
    for col in df.columns[1:]:
        g = normalize_garage(col)
        if g in GARAGE_ORDER and g not in col_por_garagem.values():
            col_por_garagem[col] = g

    blocos = [
        pd.DataFrame({
            "garagem": g,
            "codigo": codigo,
            "est_max": pd.to_numeric(df[col], errors="coerce").fillna(0),
        })
        for col, g in col_por_garagem.items()
    ]

    out = pd.concat(blocos, ignore_index=True)
    out = out.dropna(subset=["codigo"])
    out["codigo"] = out["codigo"].astype(int)
    return out[["garagem", "codigo", "est_max"]]


def calcular_romaneios(saldo_df: pd.DataFrame, maxmin_df: pd.DataFrame) -> pd.DataFrame:
    merged = saldo_df.merge(maxmin_df, on=["garagem", "codigo"], how="inner")

    merged["sobra"] = (merged["saldo"] - merged["est_max"]).clip(lower=0)
    merged["falta"] = (merged["est_max"] - merged["saldo"]).clip(lower=0)

    # Only garages in our defined order
    merged = merged[merged["garagem"].isin(GARAGE_ORDER)]

    romaneios = []

    for product_code, group in merged.groupby("codigo"):
        descricao = group["descricao"].iloc[0]
        saldo_por_garagem = group.set_index("garagem")["saldo"].to_dict()
        estmax_por_garagem = group.set_index("garagem")["est_max"].to_dict()

        surplus = (
            group[group["sobra"] > 0]
            .set_index("garagem")["sobra"]
            .to_dict()
        )
        deficit = (
            group[group["falta"] > 0]
            .set_index("garagem")["falta"]
            .to_dict()
        )

        if not surplus or not deficit:
            continue

        deficit_ordered = [g for g in GARAGE_ORDER if g in deficit]

        for from_g, sobra_restante in list(surplus.items()):
            for to_g in deficit_ordered:
                if to_g == from_g or sobra_restante <= 0:
                    continue
                falta_restante = deficit.get(to_g, 0)
                if falta_restante <= 0:
                    continue
                transfer = min(sobra_restante, falta_restante)
                romaneios.append({
                    "De": from_g,
                    "Para": to_g,
                    "Código": product_code,
                    "Produto": descricao,
                    "Saldo Origem": int(saldo_por_garagem.get(from_g, 0)),
                    "Est. Máx Origem": int(estmax_por_garagem.get(from_g, 0)),
                    "Saldo Destino": int(saldo_por_garagem.get(to_g, 0)),
                    "Est. Máx Destino": int(estmax_por_garagem.get(to_g, 0)),
                    "Quantidade": int(transfer),
                })
                sobra_restante -= transfer
                surplus[from_g] = sobra_restante
                deficit[to_g] = falta_restante - transfer

    return pd.DataFrame(romaneios)


# ── UI ────────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Romaneios entre Garagens", layout="wide")
st.title("Sugestão de Romaneios entre Garagens")
st.caption("Distribui o excesso de estoque das garagens com sobra para as garagens com déficit, priorizando G1 → G2 → G5 → G6 → G7 → SS.")

st.caption("Saldo e estoque máximo/mínimo são lidos automaticamente das planilhas Google publicadas.")

if st.button("Calcular Romaneios", type="primary"):
    with st.spinner("Processando..."):
        try:
            saldo_df = parse_saldo(load_saldo_raw())
            maxmin_df = parse_maxmin(load_maxmin_raw())
            resultado = calcular_romaneios(saldo_df, maxmin_df)
        except Exception as e:
            st.error(f"Erro ao processar os dados: {e}")
            st.stop()

    if resultado.empty:
        st.success("Nenhuma transferência necessária — todos os estoques estão dentro dos limites.")
    else:
        m1, m2, m3 = st.columns(3)
        m1.metric("Total de transferências", len(resultado))
        m2.metric("Produtos afetados", resultado["Código"].nunique())
        m3.metric("Garagens envolvidas", resultado[["De", "Para"]].stack().nunique())

        st.divider()

        f1, f2 = st.columns(2)
        with f1:
            filtro_de = st.multiselect("Filtrar por garagem de origem (De)", sorted(resultado["De"].unique()))
        with f2:
            filtro_para = st.multiselect("Filtrar por garagem de destino (Para)", sorted(resultado["Para"].unique()))

        df_view = resultado.copy()
        if filtro_de:
            df_view = df_view[df_view["De"].isin(filtro_de)]
        if filtro_para:
            df_view = df_view[df_view["Para"].isin(filtro_para)]

        st.dataframe(
            df_view,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Código": st.column_config.NumberColumn(format="%d"),
                "Saldo Origem": st.column_config.NumberColumn(format="%d"),
                "Est. Máx Origem": st.column_config.NumberColumn(format="%d"),
                "Saldo Destino": st.column_config.NumberColumn(format="%d"),
                "Est. Máx Destino": st.column_config.NumberColumn(format="%d"),
                "Quantidade": st.column_config.NumberColumn(format="%d"),
            },
        )
