import streamlit as st
import pandas as pd
import re
import unicodedata

GARAGE_ORDER = ["G1", "G2", "G5", "G6", "G7", "SS"]

# AGEs fora das abas Romaneios e Distribuição máx=0 por padrão (checkbox reinclui)
AGE_FORA_ROMANEIOS = ("SET", "ORD", "LPZ")
CORTES_DDE = ["Todos", "> 30", "> 45", "> 60", "> 75", "> 90", "> 180"]
CORTES_DDE_DEST = ["Todos", "< 7", "< 15", "< 30", "< 45", "< 60"]

# Aba Entradas (compras por semana) oculta por ora; True volta a mostrá-la e a
# ler a planilha de entradas_url
MOSTRAR_ABA_ENTRADAS = False


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


# O CSV publicado sai no formato pt-BR da planilha: "." separa milhar e ","
# separa decimal ("2.930" = 2930; "1.234,56" = 1234.56). Ler "2.930" como
# float direto dá 2,93 — foi o que zerava consumos/saldos/máximos >= 1000.
def _numero_br(serie: pd.Series) -> pd.Series:
    texto = (
        serie.fillna("").astype(str).str.strip()
        .str.replace(".", "", regex=False)
        .str.replace(",", ".", regex=False)
    )
    return pd.to_numeric(texto, errors="coerce")


def _float_br(texto: str) -> float:
    """Versão escalar de _numero_br (usada linha a linha no parse do saldo)."""
    try:
        return float(str(texto).strip().replace(".", "").replace(",", "."))
    except ValueError:
        return 0.0


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
        qty = _float_br(row_vals[10]) if len(row_vals) > 10 else 0.0

        # Total stock value (col L, index 11)
        valor = _float_br(row_vals[11]) if len(row_vals) > 11 else 0.0

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

    codigo = _numero_br(df.iloc[:, 0])

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
            "est_max": _numero_br(df[col]).fillna(0),
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
        codigo = _numero_br(dados.iloc[:, col_codigo])
        for c in range(ini, fim):
            g = normalize_garage(cabecalhos[c])
            if g not in GARAGE_ORDER:
                continue
            qtd = _numero_br(dados.iloc[:, c]).fillna(0)
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


@st.cache_data(ttl=300, show_spinner=False)
def load_entradas_raw() -> pd.DataFrame:
    """Lê a aba publicada de entradas (CSV) de st.secrets['entradas_url']."""
    try:
        url = st.secrets["entradas_url"]
    except (KeyError, FileNotFoundError):
        raise RuntimeError(
            "Secret 'entradas_url' não encontrado. Defina a URL da planilha "
            "Google publicada nos secrets do Streamlit."
        )
    return pd.read_csv(_to_csv_url(url), dtype=str)


def _sem_acento(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", str(texto)) if not unicodedata.combining(c)
    )


def parse_entradas(df: pd.DataFrame) -> pd.DataFrame:
    """Entradas por semana/depósito: Semana, depósito, código, Produto, transação,
    movimentação (quantidade). A planilha traz uma linha de total logo abaixo do
    cabeçalho, sem semana — descartada aqui junto com qualquer linha sem data.
    'Entrada por Transferência' é romaneio; o resto é compra."""
    df = df.copy()
    df.columns = [_sem_acento(c).strip().lower() for c in df.columns]

    def coluna(prefixo: str) -> pd.Series:
        for c in df.columns:
            if c.startswith(prefixo):
                return df[c]
        raise RuntimeError(f"Coluna '{prefixo}…' não encontrada na aba de entradas.")

    transacao = coluna("transa").fillna("")
    out = pd.DataFrame({
        "semana": pd.to_datetime(coluna("semana"), format="%d/%m/%Y", errors="coerce"),
        "garagem": coluna("dep").fillna("").map(normalize_garage),
        "codigo": _numero_br(coluna("cod")),
        "tipo": transacao.str.contains("transfer", case=False).map(
            {True: "romaneio", False: "compra"}
        ),
        "qtd": _numero_br(coluna("movimenta")).fillna(0),
    })
    out = out.dropna(subset=["semana", "codigo"])
    out["codigo"] = out["codigo"].astype(int)
    return out


def compras_romaneio(
    entradas: pd.DataFrame, saldo_df: pd.DataFrame, codigos
) -> pd.DataFrame:
    """Compras (entrada NF) dos itens com romaneio sugerido, uma linha por
    transação: semana, garagem, codigo, qtd e valor.

    A planilha de entradas traz quantidade, não valor: o valor é a quantidade
    vezes o preço unitário da garagem (valor ÷ saldo do relatório de saldo), com
    o preço médio do código como reserva quando a garagem não tem o item.
    """
    df = entradas[
        (entradas["tipo"] == "compra")
        & entradas["codigo"].isin(set(codigos))
        & entradas["garagem"].isin(GARAGE_ORDER)
    ].copy()

    com_saldo = saldo_df[saldo_df["saldo"] > 0].assign(
        preco=lambda d: d["valor"] / d["saldo"]
    )
    por_garagem = com_saldo.groupby(["garagem", "codigo"])["preco"].mean()
    por_codigo = com_saldo.groupby("codigo")["preco"].mean()

    chave = pd.MultiIndex.from_arrays([df["garagem"], df["codigo"]])
    df["preco"] = por_garagem.reindex(chave).to_numpy()
    df["preco"] = df["preco"].fillna(df["codigo"].map(por_codigo)).fillna(0.0)
    df["valor"] = df["qtd"] * df["preco"]
    return df[["semana", "garagem", "codigo", "qtd", "valor"]]


@st.cache_data(ttl=300, show_spinner=False)
def load_excecao_raw() -> pd.DataFrame:
    """Lê a aba publicada da lista de exceção (CSV) de st.secrets['lista_excecao']."""
    try:
        url = st.secrets["lista_excecao"]
    except (KeyError, FileNotFoundError):
        raise RuntimeError(
            "secret 'lista_excecao' não encontrado. Defina a URL da aba "
            "lista_exceção publicada nos secrets do Streamlit."
        )
    return pd.read_csv(_to_csv_url(url), dtype=str)


def parse_excecao(df: pd.DataFrame) -> frozenset:
    """Lista de exceção com colunas código e depósito. Cada par (garagem, código)
    não envia o item em romaneio. Depósito vazio vale para todas as garagens."""
    df = df.copy()
    df.columns = [_sem_acento(c).strip().lower() for c in df.columns]
    col_cod = next((c for c in df.columns if c.startswith("cod")), None)
    col_dep = next((c for c in df.columns if c.startswith("dep")), None)
    if col_cod is None or col_dep is None:
        # Sem gid, a URL publicada devolve a 1ª aba da planilha (o saldo)
        raise RuntimeError(
            "colunas 'código' e 'depósito' não encontradas. Confira se a URL do "
            "secret 'lista_excecao' aponta para a aba lista_exceção (com gid=...)."
        )

    pares = set()
    for codigo, deposito in zip(_numero_br(df[col_cod]), df[col_dep].fillna("")):
        if pd.isna(codigo):
            continue
        garagem = normalize_garage(deposito).upper()
        for g in [garagem] if garagem else GARAGE_ORDER:
            pares.add((g, int(codigo)))
    return frozenset(pares)


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


def calcular_romaneios(
    saldo_df: pd.DataFrame,
    maxmin_df: pd.DataFrame,
    consumo_df: pd.DataFrame,
    nao_envia: frozenset = frozenset(),
):
    """Retorna (romaneios, itens_sem_destino).

    Duas fases por produto:
      (a) garagens com máx=0 e saldo>0 são esvaziadas, e o saldo é redistribuído
          proporcionalmente ao máximo das demais (ocupação final igual);
      (b) regra normal sobre os saldos já atualizados: sobra → falta, seguindo a
          prioridade de GARAGE_ORDER.
    Produtos com saldo em máx=0 e nenhuma garagem com máx>0 não têm destino
    possível e vão para a lista `itens_sem_destino`.

    `nao_envia` traz os pares (garagem, codigo) da lista de exceção: essa
    garagem não envia o item em nenhuma das fases, mas ainda pode recebê-lo.
    """
    # Parte do máx/mín: o relatório de saldo omite garagens com estoque zerado, e
    # com um inner join elas sumiam do cálculo e nunca recebiam romaneio. Ficam
    # só os produtos com saldo em alguma garagem — sem saldo não há o que mover.
    merged = maxmin_df.merge(saldo_df, on=["garagem", "codigo"], how="left")
    merged = merged[
        merged["garagem"].isin(GARAGE_ORDER) & merged["codigo"].isin(saldo_df["codigo"])
    ]
    merged[["saldo", "valor"]] = merged[["saldo", "valor"]].fillna(0.0)

    # (garagem, codigo) → (c3m, c6m); ausente na aba de consumo = 0
    consumo = {
        (g, c): (c3, c6)
        for g, c, c3, c6 in consumo_df[["garagem", "codigo", "c3m", "c6m"]].itertuples(index=False)
    }

    romaneios = []
    sem_destino = []

    for product_code, group in merged.groupby("codigo"):
        # Garagens incluídas com saldo 0 não têm descrição (vem do relatório de saldo)
        descricao = group["descricao"].dropna().iloc[0]
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
            c6m_dest = consumo.get((para, product_code), (0, 0))[1]
            romaneios.append({
                "De": de,
                "Para": para,
                "Código": product_code,
                "Produto": descricao,
                "Saldo Origem": int(saldo_orig.get(de, 0)),
                "Est. Máx Origem": int(est_max.get(de, 0)),
                # Dias de estoque: saldo / consumo diário médio de 6m (180 dias),
                # cada um com o consumo da sua garagem. Sem consumo não há DDE —
                # fica vazio (estoque que não gira).
                "DDE orig": round(saldo_orig.get(de, 0) / (c6m / 180)) if c6m > 0 else None,
                "c3m": int(c3m),
                "c6m": int(c6m),
                "Saldo Destino": int(saldo_orig.get(para, 0)),
                "Est. Máx Destino": int(est_max.get(para, 0)),
                "DDE dest": (
                    round(saldo_orig.get(para, 0) / (c6m_dest / 180)) if c6m_dest > 0 else None
                ),
                "Quantidade": int(qtd),
                "Valor Transferido": round(preco_unit.get(de, 0.0) * qtd, 2),
                "age": age,
                "pqr": pqr,
                "regra": regra,
            })

        saldo_atual = dict(saldo_orig)

        # ── (a) evacuação das garagens com máx = 0 ──────────────────────────
        # Garagens da lista de exceção não enviam este item (mas podem receber).
        # Numa garagem de máx=0 o saldo fica parado: nem evacua, nem vai para
        # "sem destino".
        bloqueadas = {g for g in saldo_atual if (g, product_code) in nao_envia}
        origens_zero = {
            g: s for g, s in saldo_atual.items()
            if est_max.get(g, 0) == 0 and s > 0 and g not in bloqueadas
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
            if est_max.get(g, 0) > 0 and saldo_atual[g] > est_max[g] and g not in bloqueadas
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
# Menos espaço acima do título (o padrão do Streamlit é ~6rem)
st.markdown(
    "<style>[data-testid='stMainBlockContainer'], .block-container "
    "{padding-top: 2rem;}</style>",
    unsafe_allow_html=True,
)
st.subheader("Sugestão de Romaneios entre Garagens")


@st.cache_data(ttl=300, show_spinner=False)
def carregar_romaneios(com_entradas: bool):
    """Lê as planilhas e calcula ao abrir a página. Mesmo cache de 5 min das
    leituras: mexer em filtros (cada um dispara um rerun) ou recarregar a página
    dentro desse prazo reaproveita o cálculo em vez de refazê-lo."""
    saldo_df = parse_saldo(load_saldo_raw())
    maxmin_df = parse_maxmin(load_maxmin_raw())
    consumo_df = parse_consumo(load_consumo_raw())

    # Lista de exceção: se faltar o secret ou a leitura falhar, calcula sem ela e
    # avisa no topo da página, em vez de derrubar o app.
    try:
        nao_envia, erro_excecao = parse_excecao(load_excecao_raw()), ""
    except Exception as e:
        nao_envia, erro_excecao = frozenset(), str(e)
    resultado, sem_destino = calcular_romaneios(saldo_df, maxmin_df, consumo_df, nao_envia)

    # A aba de entradas é extra: se faltar o secret ou a leitura falhar, o resto
    # do app continua funcionando e a aba Entradas mostra o motivo.
    # `com_entradas` vem como argumento (não lido da constante) para entrar na
    # chave do cache: ligar a aba não pode reaproveitar um resultado sem entradas.
    entradas, erro_entradas = pd.DataFrame(), ""
    if com_entradas:
        try:
            entradas = compras_romaneio(
                parse_entradas(load_entradas_raw()), saldo_df, resultado["Código"].unique()
            )
        except Exception as e:
            erro_entradas = str(e)

    return resultado, sem_destino, entradas, erro_entradas, erro_excecao


with st.spinner("Carregando planilhas e calculando romaneios..."):
    try:
        resultado, sem_destino, entradas, erro_entradas, erro_excecao = carregar_romaneios(
            MOSTRAR_ABA_ENTRADAS
        )
    except Exception as e:
        st.error(f"Erro ao processar os dados: {e}")
        st.stop()

if erro_excecao:
    st.warning(f"Lista de exceção não aplicada: {erro_excecao}")

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
    # Mesmo padrão de cabeçalhos curtos da tabela de romaneios
    cabecalhos = {
        "Código": "código",
        "Produto": "produto",
        "age": "AGE",
        "Garagem": "garagem",
        "Saldo": "sld",
        "Valor em Estoque": "valor",
    }
    st.dataframe(
        sem_destino[list(cabecalhos)].rename(columns=cabecalhos),
        use_container_width=True,
        hide_index=True,
        column_config={
            "código": st.column_config.NumberColumn(format="%d", alignment="center"),
            "produto": st.column_config.Column(alignment="left"),
            "AGE": st.column_config.Column(alignment="center"),
            "garagem": st.column_config.Column(alignment="center"),
            "sld": st.column_config.NumberColumn(format="%d"),
            # step=0.1 fixa 1 casa decimal no formato localized
            "valor": st.column_config.NumberColumn(
                format="localized", step=0.1, help="Valor em estoque (R$)"
            ),
        },
    )


def _tabela_semanal(
    celulas: pd.DataFrame, total_semana: pd.Series, total_garagem: pd.Series, total_geral
) -> pd.DataFrame:
    """Semana nas linhas, garagem nas colunas, com coluna e linha de Total. Os
    totais vêm prontos porque nem sempre são a soma das células: em produtos
    distintos, um código comprado por duas garagens conta uma vez no total."""
    t = celulas.copy()
    t["Total"] = total_semana
    t.index = t.index.strftime("%d/%m/%Y")
    t.loc["Total"] = list(total_garagem.reindex(celulas.columns, fill_value=0)) + [total_geral]
    t.columns.name = None
    return t.rename_axis("semana").reset_index()


def render_entradas(compras: pd.DataFrame, erro: str) -> None:
    st.caption(
        "Compras (entrada NF) por semana dos itens com romaneio sugerido nas abas "
        "Romaneios e Distribuição máx=0. Valor = quantidade × preço unitário da "
        "garagem (valor ÷ saldo do relatório de saldo)."
    )
    if erro:
        st.warning(f"Não foi possível ler a aba de entradas: {erro}")
        return
    if compras.empty:
        st.info("Nenhuma compra dos itens com romaneio sugerido no período.")
        return

    m1, m2, m3 = st.columns(3)
    m1.metric("Valor comprado", formatar_brl(compras["valor"].sum()))
    m2.metric("Produtos comprados", compras["codigo"].nunique())
    m3.metric("Transações de compra", len(compras))

    garagens = [g for g in GARAGE_ORDER if g in set(compras["garagem"])]
    por_semana = compras.groupby("semana")
    por_garagem = compras.groupby("garagem")

    def pivo(coluna: str, agregacao: str) -> pd.DataFrame:
        return compras.pivot_table(
            index="semana", columns="garagem", values=coluna, aggfunc=agregacao, fill_value=0
        ).reindex(columns=garagens, fill_value=0)

    valor = _tabela_semanal(
        pivo("valor", "sum"),
        por_semana["valor"].sum(),
        por_garagem["valor"].sum(),
        compras["valor"].sum(),
    )
    produtos = _tabela_semanal(
        pivo("codigo", "nunique"),
        por_semana["codigo"].nunique(),
        por_garagem["codigo"].nunique(),
        compras["codigo"].nunique(),
    )

    semana = st.column_config.Column(alignment="center")
    reais = st.column_config.NumberColumn(format="localized", step=0.1)
    inteiro = st.column_config.NumberColumn(format="%d")
    colunas = garagens + ["Total"]

    t1, t2 = st.columns(2)
    with t1:
        st.markdown("**Valor comprado (R$)**")
        st.dataframe(
            valor,
            use_container_width=True,
            hide_index=True,
            column_config={"semana": semana, **{c: reais for c in colunas}},
        )
    with t2:
        st.markdown(
            "**Produtos comprados**",
            help="Códigos diferentes comprados. No Total, um código comprado por "
            "mais de uma garagem conta uma vez.",
        )
        st.dataframe(
            produtos,
            use_container_width=True,
            hide_index=True,
            column_config={"semana": semana, **{c: inteiro for c in colunas}},
        )


def _sem_age_fora(df: pd.DataFrame, age_fora: tuple) -> pd.DataFrame:
    if df.empty or not age_fora:
        return df
    return df[~df["age"].str.strip().str.upper().isin(age_fora)]


def render_romaneios(
    resultado: pd.DataFrame,
    chave: str,
    msg_vazio: str,
    age_fora: tuple = (),
    filtro_dde: bool = False,
) -> None:
    """`chave` prefixa os widgets: a mesma função roda em duas abas e o Streamlit
    exige IDs únicos por widget. `age_fora` esconde essas AGEs por padrão (com
    checkbox para reincluir); `filtro_dde` mostra o corte por DDE da origem."""
    if resultado.empty:
        st.success(msg_vazio)
        return

    # Todos os filtros numa linha; a última coluna fica vazia só para estreitar os
    # dropdowns. O checkbox é preenchido primeiro (mesmo estando à direita)
    # porque a exclusão de AGE muda as opções do filtro "age".
    f_de, f_para, f_age, f_pqr, f_dde, f_dde_dest, f_todas, _ = st.columns(
        [1, 1, 1, 1, 1, 1, 1.2, 0.8], vertical_alignment="bottom"
    )
    if age_fora:
        with f_todas:
            todas_age = st.checkbox(
                "Incluir todas as AGE",
                key=f"{chave}_todas_age",
                help=f"Por padrão ficam fora: {', '.join(age_fora)}",
            )
        if not todas_age:
            resultado = _sem_age_fora(resultado, age_fora)

    corte_dde = corte_dde_dest = "Todos"
    if filtro_dde:
        with f_dde:
            corte_dde = st.selectbox(
                "DDE orig",
                CORTES_DDE,
                key=f"{chave}_dde",
                help="Dias de estoque da origem. Itens sem consumo em 6 meses "
                "(DDE vazio) entram em qualquer corte",
            )
        with f_dde_dest:
            corte_dde_dest = st.selectbox(
                "DDE dest",
                CORTES_DDE_DEST,
                key=f"{chave}_dde_dest",
                help="Dias de estoque do destino, pelo consumo do próprio destino. "
                "Itens sem consumo em 6 meses (DDE vazio) ficam fora destes cortes",
            )

    def dropdown(rotulo: str, coluna: str, sufixo: str) -> list:
        return st.multiselect(
            rotulo, sorted(resultado[coluna].unique()), key=f"{chave}_{sufixo}", placeholder="Todos"
        )

    with f_de:
        filtro_de = dropdown("Origem (De)", "De", "de")
    with f_para:
        filtro_para = dropdown("Destino (Para)", "Para", "para")
    with f_age:
        filtro_age = dropdown("age", "age", "age")
    with f_pqr:
        filtro_pqr = dropdown("pqr", "pqr", "pqr")

    df_view = resultado.copy()
    if filtro_de:
        df_view = df_view[df_view["De"].isin(filtro_de)]
    if filtro_para:
        df_view = df_view[df_view["Para"].isin(filtro_para)]
    if filtro_age:
        df_view = df_view[df_view["age"].isin(filtro_age)]
    if filtro_pqr:
        df_view = df_view[df_view["pqr"].isin(filtro_pqr)]
    if corte_dde != "Todos":
        dias = int(corte_dde.lstrip("> "))
        # DDE vazio = sem consumo em 6 meses: o estoque não gira, passa em qualquer corte
        df_view = df_view[df_view["DDE orig"].isna() | (df_view["DDE orig"] > dias)]
    if corte_dde_dest != "Todos":
        # Aqui o vazio fica de fora: sem consumo, o estoque do destino nunca acaba
        df_view = df_view[df_view["DDE dest"] < int(corte_dde_dest.lstrip("< "))]

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
            "DDE orig": "DDE orig",
            "c3m": "c3m",
            "c6m": "c6m",
            "Saldo Destino": "sld dest",
            "Est. Máx Destino": "máx dest",
            "DDE dest": "DDE dest",
            "Quantidade": "qtde",
            "Valor Transferido": "valor",
        }
        # Alinhamento: centro = de, para, código, AGE; esquerda = produto;
        # direita = demais (padrão das colunas numéricas)
        inteiro = st.column_config.NumberColumn(format="%d")
        centro = st.column_config.Column(alignment="center")
        st.dataframe(
            df_view[list(cabecalhos)].rename(columns=cabecalhos),
            use_container_width=True,
            hide_index=True,
            column_config={
                "de": centro,
                "para": centro,
                "código": st.column_config.NumberColumn(format="%d", alignment="center"),
                "produto": st.column_config.Column(alignment="left"),
                "AGE": centro,
                "sld orig": inteiro,
                "máx orig": inteiro,
                "DDE orig": st.column_config.NumberColumn(
                    format="%d",
                    help="Dias de estoque da origem (saldo ÷ consumo diário de 6 meses). "
                    "Vazio = sem consumo em 6 meses",
                ),
                "DDE dest": st.column_config.NumberColumn(
                    format="%d",
                    help="Dias de estoque do destino, pelo consumo do próprio destino. "
                    "Vazio = sem consumo em 6 meses",
                ),
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


if resultado.empty:
    rom_normal = rom_evacuacao = resultado
else:
    rom_normal = resultado[resultado["regra"] == "normal"]
    rom_evacuacao = resultado[resultado["regra"] == "evacuacao"]


# O checkbox fica dentro da aba, mas o rótulo é montado antes: lê o estado
# do widget (persistido em session_state) para a contagem bater com a tabela.
def qtd_aba(df: pd.DataFrame, chave: str) -> int:
    if st.session_state.get(f"{chave}_todas_age", False):
        return len(df)
    return len(_sem_age_fora(df, AGE_FORA_ROMANEIOS))


abas = st.tabs([
    f"Romaneios ({qtd_aba(rom_normal, 'normal')})",
    f"Distribuição máx=0 ({qtd_aba(rom_evacuacao, 'evacuacao')})",
    f"Itens sem destino ({len(sem_destino)})",
] + (["Entradas"] if MOSTRAR_ABA_ENTRADAS else []))
aba_normal, aba_evacuacao, aba_sem_destino = abas[:3]

with aba_normal:
    st.caption(
        "Transferências da regra padrão: garagens com saldo acima do máximo "
        "abastecem as que estão abaixo, na prioridade G1 → G2 → G5 → G6 → G7 → SS."
    )
    render_romaneios(
        rom_normal,
        "normal",
        "Nenhuma transferência necessária — todos os estoques estão dentro dos limites.",
        age_fora=AGE_FORA_ROMANEIOS,
        filtro_dde=True,
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
        age_fora=AGE_FORA_ROMANEIOS,
        filtro_dde=True,
    )

with aba_sem_destino:
    render_sem_destino(sem_destino)

if MOSTRAR_ABA_ENTRADAS:
    with abas[3]:
        render_entradas(entradas, erro_entradas)
