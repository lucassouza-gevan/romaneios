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


def formatar_brl(valor: float) -> str:
    """1234567.5 → 'R$ 1.234.567,50' (separadores no padrão brasileiro)."""
    inteiro, decimal = f"{valor:,.2f}".split(".")
    return f"R$ {inteiro.replace(',', '.')},{decimal}"


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

        # Total stock value (col L, index 11)
        try:
            valor_raw = row_vals[11] if len(row_vals) > 11 else ""
            valor = float(valor_raw.replace(",", ".")) if valor_raw else 0.0
        except ValueError:
            valor = 0.0

        rows.append({
            "garagem": current_garage,
            "codigo": product_code,
            "descricao": description,
            "saldo": qty,
            "valor": valor,
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
    """Formato wide: 1ª coluna = código; depois metadados (age, pqr, rev) e um
    bloco de colunas por garagem com o estoque MÁXIMO. Usa a primeira ocorrência
    de cada garagem (bloco máximo) e ignora o bloco de mínimo — o cálculo só usa
    o máximo. As colunas age/pqr são preservadas para servirem de filtro."""
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    codigo = pd.to_numeric(df.iloc[:, 0], errors="coerce")

    def coluna_meta(nome: str) -> pd.Series:
        for c in df.columns:
            if c.lower() == nome:
                return df[c].fillna("").astype(str).str.strip()
        return pd.Series([""] * len(df), index=df.index)

    age = coluna_meta("age")
    pqr = coluna_meta("pqr")

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
            "age": age,
            "pqr": pqr,
        })
        for col, g in col_por_garagem.items()
    ]

    out = pd.concat(blocos, ignore_index=True)
    out = out.dropna(subset=["codigo"])
    out["codigo"] = out["codigo"].astype(int)
    return out[["garagem", "codigo", "est_max", "age", "pqr"]]


@st.cache_data(ttl=300, show_spinner=False)
def load_consumo_raw() -> pd.DataFrame:
    """Lê a aba publicada de consumo 3m/6m (CSV) de st.secrets['consumo_url']."""
    try:
        url = st.secrets["consumo_url"]
    except (KeyError, FileNotFoundError):
        raise RuntimeError(
            "Secret 'consumo_url' não encontrado. Defina a URL da planilha "
            "Google publicada nos secrets do Streamlit."
        )
    return pd.read_csv(_to_csv_url(url), header=None, dtype=str)


def parse_consumo(df_raw: pd.DataFrame) -> pd.DataFrame:
    """Aba com blocos lado a lado: linha 1 traz o título do bloco ("consumo 06
    meses", "consumo 03 meses") e linha 2 os cabeçalhos (código, produto, G1…SS).
    Cada bloco vai da coluna do título até o início do próximo. Retorna formato
    long: garagem, codigo, c3m, c6m."""
    titulos = df_raw.iloc[0].fillna("").astype(str).str.strip()
    cabecalhos = df_raw.iloc[1].fillna("").astype(str).str.strip()
    dados = df_raw.iloc[2:]

    inicios = []
    for i, t in enumerate(titulos):
        m = re.search(r"consumo\s*0*(\d+)\s*mes", t, re.IGNORECASE)
        if m:
            inicios.append((i, f"c{int(m.group(1))}m"))

    blocos = []
    for n, (ini, nome) in enumerate(inicios):
        fim = inicios[n + 1][0] if n + 1 < len(inicios) else len(cabecalhos)
        col_codigo = next(
            (c for c in range(ini, fim) if cabecalhos[c].lower() in ("código", "codigo")),
            None,
        )
        if col_codigo is None:
            continue
        codigo = pd.to_numeric(dados.iloc[:, col_codigo], errors="coerce")
        for c in range(ini, fim):
            g = normalize_garage(cabecalhos[c])
            if g not in GARAGE_ORDER:
                continue
            qtd = pd.to_numeric(
                dados.iloc[:, c].fillna("").str.replace(",", "."), errors="coerce"
            ).fillna(0)
            blocos.append(pd.DataFrame({
                "garagem": g, "codigo": codigo, "periodo": nome, "qtd": qtd,
            }))

    if not blocos:
        return pd.DataFrame(columns=["garagem", "codigo", "c3m", "c6m"])

    out = pd.concat(blocos, ignore_index=True).dropna(subset=["codigo"])
    out["codigo"] = out["codigo"].astype(int)
    out = out.pivot_table(
        index=["garagem", "codigo"], columns="periodo", values="qtd", aggfunc="sum"
    ).reset_index()
    for col in ("c3m", "c6m"):
        if col not in out.columns:
            out[col] = 0.0
    return out[["garagem", "codigo", "c3m", "c6m"]].fillna(0)


def _arredondar_maior_resto(alocacao: dict, total: int) -> dict:
    """Converte alocações fracionárias em inteiros somando exatamente `total`,
    distribuindo o resto pelas maiores partes fracionárias."""
    base = {g: int(v) for g, v in alocacao.items()}
    resto = total - sum(base.values())
    if resto > 0:
        por_fracao = sorted(alocacao, key=lambda g: alocacao[g] - int(alocacao[g]), reverse=True)
        for g in por_fracao[:resto]:
            base[g] += 1
    return {g: v for g, v in base.items() if v > 0}


def _distribuir_proporcional(total: int, destinos: dict) -> dict:
    """Distribui `total` unidades entre `destinos` ({garagem: (saldo, est_max)},
    est_max > 0) de forma que a ocupação final (saldo/máx) fique igual para todas.

    Quem já está acima do alvo proporcional não recebe e sai do rateio (nunca
    tiramos estoque de ninguém) — os demais reabsorvem o total. Pode estourar o
    máximo do destino quando o estoque total supera a soma dos máximos.
    """
    elegiveis = dict(destinos)
    alocacao = {g: 0.0 for g in destinos}

    while elegiveis:
        soma_max = sum(m for _, m in elegiveis.values())
        if soma_max <= 0:
            break
        soma_saldo = sum(s for s, _ in elegiveis.values())
        k = (soma_saldo + total) / soma_max
        acima = [g for g, (s, m) in elegiveis.items() if k * m < s]
        if not acima:
            for g, (s, m) in elegiveis.items():
                alocacao[g] = k * m - s
            break
        for g in acima:
            elegiveis.pop(g)

    return _arredondar_maior_resto(alocacao, total)


def calcular_romaneios(saldo_df: pd.DataFrame, maxmin_df: pd.DataFrame, consumo_df: pd.DataFrame):
    """Retorna (romaneios, itens_sem_destino).

    Duas fases por produto:
      (a) garagens com máx=0 e saldo>0 são esvaziadas, e o saldo é redistribuído
          proporcionalmente ao máximo das demais (ocupação final igual);
      (b) regra normal sobre os saldos já atualizados: sobra → falta, seguindo a
          prioridade de GARAGE_ORDER.
    Produtos com saldo em máx=0 e nenhuma garagem com máx>0 não têm destino
    possível e vão para a lista `itens_sem_destino`.
    """
    merged = saldo_df.merge(maxmin_df, on=["garagem", "codigo"], how="inner")
    merged = merged[merged["garagem"].isin(GARAGE_ORDER)]

    # (garagem, codigo) → (c3m, c6m); ausente na aba de consumo = 0
    consumo = {
        (g, c): (c3, c6)
        for g, c, c3, c6 in consumo_df[["garagem", "codigo", "c3m", "c6m"]].itertuples(index=False)
    }

    romaneios = []
    sem_destino = []

    for product_code, group in merged.groupby("codigo"):
        descricao = group["descricao"].iloc[0]
        age = group["age"].iloc[0]
        pqr = group["pqr"].iloc[0]

        saldo_orig = group.set_index("garagem")["saldo"].to_dict()
        est_max = group.set_index("garagem")["est_max"].to_dict()
        valor = group.set_index("garagem")["valor"].to_dict()

        # Preço unitário a partir do estado original (valor total / saldo)
        preco_unit = {
            g: (valor.get(g, 0.0) / s if s else 0.0) for g, s in saldo_orig.items()
        }

        def registrar(de, para, qtd, regra):
            c3m, c6m = consumo.get((de, product_code), (0, 0))
            romaneios.append({
                "De": de,
                "Para": para,
                "Código": product_code,
                "Produto": descricao,
                "Saldo Origem": int(saldo_orig.get(de, 0)),
                "Est. Máx Origem": int(est_max.get(de, 0)),
                "c3m": int(c3m),
                "c6m": int(c6m),
                "Saldo Destino": int(saldo_orig.get(para, 0)),
                "Est. Máx Destino": int(est_max.get(para, 0)),
                "Quantidade": int(qtd),
                "Valor Transferido": round(preco_unit.get(de, 0.0) * qtd, 2),
                "age": age,
                "pqr": pqr,
                "regra": regra,
            })

        saldo_atual = dict(saldo_orig)

        # ── (a) evacuação das garagens com máx = 0 ──────────────────────────
        origens_zero = {
            g: s for g, s in saldo_atual.items() if est_max.get(g, 0) == 0 and s > 0
        }
        # Lista (não set) e na ordem canônica: a ordem influencia o desempate do
        # arredondamento, e um set daria resultados diferentes a cada execução.
        destinos_validos = [
            g for g in GARAGE_ORDER if g in saldo_atual and est_max.get(g, 0) > 0
        ]

        if origens_zero and not destinos_validos:
            for g, s in origens_zero.items():
                sem_destino.append({
                    "Código": product_code,
                    "Produto": descricao,
                    "Garagem": g,
                    "Saldo": int(s),
                    "Valor em Estoque": round(valor.get(g, 0.0), 2),
                    "age": age,
                    "pqr": pqr,
                })
        elif origens_zero:
            total_evacuar = int(sum(origens_zero.values()))
            destinos = {g: (saldo_atual[g], est_max[g]) for g in destinos_validos}
            alocacao = _distribuir_proporcional(total_evacuar, destinos)

            for orig in [g for g in GARAGE_ORDER if g in origens_zero]:
                restante = int(origens_zero[orig])
                for dest in [g for g in GARAGE_ORDER if g in alocacao]:
                    if restante <= 0:
                        break
                    qtd = min(restante, alocacao[dest])
                    if qtd <= 0:
                        continue
                    registrar(orig, dest, qtd, "evacuacao")
                    alocacao[dest] -= qtd
                    restante -= qtd
                    saldo_atual[dest] += qtd
                saldo_atual[orig] = restante

        # ── (b) regra normal: sobra → falta, com os saldos já atualizados ───
        surplus = {
            g: saldo_atual[g] - est_max[g]
            for g in saldo_atual
            if est_max.get(g, 0) > 0 and saldo_atual[g] > est_max[g]
        }
        deficit = {
            g: est_max[g] - saldo_atual[g]
            for g in saldo_atual
            if est_max.get(g, 0) > 0 and saldo_atual[g] < est_max[g]
        }

        if not surplus or not deficit:
            continue

        deficit_ordered = [g for g in GARAGE_ORDER if g in deficit]

        for from_g in [g for g in GARAGE_ORDER if g in surplus]:
            sobra_restante = surplus[from_g]
            for to_g in deficit_ordered:
                if to_g == from_g or sobra_restante <= 0:
                    continue
                falta_restante = deficit.get(to_g, 0)
                if falta_restante <= 0:
                    continue
                transfer = min(sobra_restante, falta_restante)
                registrar(from_g, to_g, transfer, "normal")
                sobra_restante -= transfer
                surplus[from_g] = sobra_restante
                deficit[to_g] = falta_restante - transfer

    df = pd.DataFrame(romaneios)
    if not df.empty:
        df = df.sort_values("Valor Transferido", ascending=False).reset_index(drop=True)
    return df, pd.DataFrame(sem_destino)


# ── UI ────────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Romaneios entre Garagens", layout="wide")
st.title("Sugestão de Romaneios entre Garagens")
st.caption("Distribui o excesso de estoque das garagens com sobra para as garagens com déficit, priorizando G1 → G2 → G5 → G6 → G7 → SS.")

st.caption("Saldo e estoque máximo/mínimo são lidos automaticamente das planilhas Google publicadas.")

# O botão apenas calcula e guarda em session_state. A renderização acontece fora
# do bloco: st.button() só é True no rerun do clique, e mexer num filtro dispara
# um novo rerun — sem isso a tabela sumiria a cada filtro aplicado.
if st.button("Calcular Romaneios", type="primary"):
    with st.spinner("Processando..."):
        try:
            saldo_df = parse_saldo(load_saldo_raw())
            maxmin_df = parse_maxmin(load_maxmin_raw())
            consumo_df = parse_consumo(load_consumo_raw())
            resultado, sem_destino = calcular_romaneios(saldo_df, maxmin_df, consumo_df)
            st.session_state.resultado = resultado
            st.session_state.sem_destino = sem_destino
        except Exception as e:
            st.error(f"Erro ao processar os dados: {e}")
            st.stop()

resultado = st.session_state.get("resultado")
sem_destino = st.session_state.get("sem_destino")

if sem_destino is None:
    sem_destino = pd.DataFrame()

def render_sem_destino(sem_destino: pd.DataFrame) -> None:
    st.caption(
        "Itens com saldo em garagens cujo estoque máximo é 0, mas sem nenhuma "
        "garagem com máximo > 0 para recebê-los — não há para onde transferir."
    )
    if sem_destino.empty:
        st.success("Nenhum item sem destino.")
        return

    s1, s2 = st.columns(2)
    s1.metric("Itens sem destino", len(sem_destino))
    s2.metric("Valor parado", formatar_brl(sem_destino["Valor em Estoque"].sum()))
    st.dataframe(
        sem_destino.drop(columns=["pqr"]),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Código": st.column_config.NumberColumn(format="%d"),
            "Saldo": st.column_config.NumberColumn(format="%d"),
            "Valor em Estoque": st.column_config.NumberColumn(
                "Valor em Estoque (R$)", format="localized"
            ),
        },
    )


def render_romaneios(resultado: pd.DataFrame, chave: str, msg_vazio: str) -> None:
    """`chave` prefixa os widgets: a mesma função roda em duas abas e o Streamlit
    exige IDs únicos por widget."""
    if resultado.empty:
        st.success(msg_vazio)
        return

    f1, f2, f3, f4 = st.columns(4)
    with f1:
        filtro_de = st.multiselect("Origem (De)", sorted(resultado["De"].unique()), key=f"{chave}_de")
    with f2:
        filtro_para = st.multiselect("Destino (Para)", sorted(resultado["Para"].unique()), key=f"{chave}_para")
    with f3:
        filtro_age = st.multiselect("age", sorted(resultado["age"].unique()), key=f"{chave}_age")
    with f4:
        filtro_pqr = st.multiselect("pqr", sorted(resultado["pqr"].unique()), key=f"{chave}_pqr")

    df_view = resultado.copy()
    if filtro_de:
        df_view = df_view[df_view["De"].isin(filtro_de)]
    if filtro_para:
        df_view = df_view[df_view["Para"].isin(filtro_para)]
    if filtro_age:
        df_view = df_view[df_view["age"].isin(filtro_age)]
    if filtro_pqr:
        df_view = df_view[df_view["pqr"].isin(filtro_pqr)]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total de transferências", len(df_view))
    m2.metric("Produtos afetados", df_view["Código"].nunique())
    m3.metric(
        "Garagens envolvidas",
        df_view[["De", "Para"]].stack().nunique() if not df_view.empty else 0,
    )
    m4.metric("Valor total movimentado", formatar_brl(df_view["Valor Transferido"].sum()))

    st.divider()

    if df_view.empty:
        st.info("Nenhum romaneio para os filtros selecionados.")
    else:
        # Nome interno → cabeçalho exibido (na ordem da tabela). Os nomes internos
        # seguem em uso nos filtros e métricas; só a exibição é renomeada.
        cabecalhos = {
            "De": "de",
            "Para": "para",
            "Código": "código",
            "Produto": "produto",
            "age": "AGE",
            "Saldo Origem": "sld orig",
            "Est. Máx Origem": "máx orig",
            "c3m": "c3m",
            "c6m": "c6m",
            "Saldo Destino": "sld dest",
            "Est. Máx Destino": "máx dest",
            "Quantidade": "qtde",
            "Valor Transferido": "valor",
        }
        inteiro = st.column_config.NumberColumn(format="%d")
        st.dataframe(
            df_view[list(cabecalhos)].rename(columns=cabecalhos),
            use_container_width=True,
            hide_index=True,
            column_config={
                "código": inteiro,
                "sld orig": inteiro,
                "máx orig": inteiro,
                "c3m": st.column_config.NumberColumn(
                    format="%d", help="Consumo da garagem de origem nos últimos 3 meses"
                ),
                "c6m": st.column_config.NumberColumn(
                    format="%d", help="Consumo da garagem de origem nos últimos 6 meses"
                ),
                "sld dest": inteiro,
                "máx dest": inteiro,
                "qtde": inteiro,
                # step=0.1 fixa 1 casa decimal no formato localized
                "valor": st.column_config.NumberColumn(
                    format="localized", step=0.1, help="Valor transferido (R$)"
                ),
            },
        )


if resultado is None:
    st.info("Clique em **Calcular Romaneios** para gerar as sugestões.")
else:
    if resultado.empty:
        rom_normal = rom_evacuacao = resultado
    else:
        rom_normal = resultado[resultado["regra"] == "normal"]
        rom_evacuacao = resultado[resultado["regra"] == "evacuacao"]

    aba_normal, aba_evacuacao, aba_sem_destino = st.tabs([
        f"Romaneios ({len(rom_normal)})",
        f"Distribuição máx=0 ({len(rom_evacuacao)})",
        f"Itens sem destino ({len(sem_destino)})",
    ])

    with aba_normal:
        st.caption(
            "Transferências da regra padrão: garagens com saldo acima do máximo "
            "abastecem as que estão abaixo, na prioridade G1 → G2 → G5 → G6 → G7 → SS."
        )
        render_romaneios(
            rom_normal,
            "normal",
            "Nenhuma transferência necessária — todos os estoques estão dentro dos limites.",
        )

    with aba_evacuacao:
        st.caption(
            "Garagens cujo estoque máximo é 0 mas têm saldo: todo o saldo é evacuado e "
            "redistribuído entre as demais, proporcionalmente ao estoque máximo de cada uma."
        )
        render_romaneios(
            rom_evacuacao,
            "evacuacao",
            "Nenhuma garagem com estoque máximo zerado e saldo em estoque.",
        )

    with aba_sem_destino:
        render_sem_destino(sem_destino)
