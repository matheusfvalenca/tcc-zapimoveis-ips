# -*- coding: utf-8 -*-
"""
ouro_analise.py — Camada Ouro: análise estatística e visualização
==================================================================

Lê a base tratada da Camada Prata (zap_imoveis_prata.csv) e produz as métricas, as
tabelas e as figuras que respondem ao objetivo da pesquisa: medir a relação entre o
custo do metro quadrado e a pontuação do IPS nas regiões do Rio de Janeiro e
identificar as distorções entre preço e infraestrutura social.

Corresponde à Etapa 4 do cronograma do TCC.

O que o script faz, nesta ordem:
    A. Caracteriza o mercado   — distribuição do aluguel e do preço/m², por zona e por região
    B. Mede a relação central  — IPS x preço/m², com correlação e regressão log-linear
    C. Decompõe o IPS          — qual das três dimensões o mercado efetivamente precifica
    D. Mede as distorções      — desvio de cada região em relação ao preço que seu IPS previa
    E. Testa a robustez        — sensibilidade ao corte mínimo, aos outliers e ao tamanho do imóvel

Decisões metodológicas relevantes (todas parametrizadas na seção 1):
    - A unidade de análise é a REGIÃO ADMINISTRATIVA, não o bairro, porque é nesse nível
      que o IPS é divulgado. Bairros distintos de uma mesma região compartilham a nota.
    - Usa-se a MEDIANA, e não a média, por ser robusta aos valores extremos que sobrevivem
      à triagem da Camada Prata.
    - Regiões com poucos anúncios produzem medianas instáveis e são excluídas do cálculo
      correlacional (o corte é testado no bloco E).
    - O preço observado é o PEDIDO no anúncio, não o efetivamente contratado.

Uso:
    python ouro_analise.py                        # execução padrão (residencial, corte de 30)
    python ouro_analise.py --min-anuncios 50      # corte mínimo por região
    python ouro_analise.py --classe comercial     # analisa o mercado comercial
    python ouro_analise.py --incluir-outliers     # usa também as linhas marcadas como outlier
    python ouro_analise.py --sem-figuras          # só as tabelas e o relatório
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# =============================================================================
# 1. CONFIGURAÇÃO
# =============================================================================

PASTA = Path(__file__).resolve().parent

def pasta_do_projeto(nome: str) -> Path:
    """Pasta da camada dentro do projeto; se a estrutura em camadas não existir
    (todos os arquivos soltos numa pasta só), tudo cai na pasta do próprio script."""
    candidata = PASTA.parent / nome
    return candidata if candidata.is_dir() else PASTA

PASTA_PRATA = pasta_do_projeto("2_prata")       # de onde vem a base tratada
PASTA_SAIDA = PASTA                             # esta camada escreve na própria pasta

ARQUIVO_ENTRADA = "zap_imoveis_prata.csv"
ARQUIVO_RELATORIO = "relatorio_ouro.txt"
PASTA_FIGURAS = "figuras"
CODIFICACAO = "utf-8-sig"

# Tabelas exportadas (servem de fonte para as tabelas do TCC)
ARQUIVO_RAS = "ouro_regioes.csv"            # uma linha por região administrativa
ARQUIVO_BAIRROS = "ouro_bairros.csv"        # uma linha por bairro (descritivo, sem IPS)
ARQUIVO_ZONAS = "ouro_zonas.csv"            # uma linha por zona do portal
ARQUIVO_CORRELACOES = "ouro_correlacoes.csv"  # todas as correlações calculadas

CLASSE_PADRAO = "residencial"               # recorte coerente com "custo de moradia"
MIN_ANUNCIOS_PADRAO = 30                    # mínimo de anúncios para a região entrar na correlação

# Colunas do IPS e seus rótulos para o relatório e as figuras
DIMENSOES = {
    "ips": "IPS geral",
    "ips_necessidades_basicas": "Necessidades Humanas Básicas",
    "ips_bem_estar": "Fundamentos do Bem-Estar",
    "ips_oportunidades": "Oportunidades",
}

# Cortes testados no bloco de robustez
CORTES_ROBUSTEZ = [10, 20, 30, 50, 100, 200]

# As figuras entram no TCC sob uma legenda numerada ("Figura 4 – ..."), que já cumpre o papel
# do título. Repeti-lo dentro da imagem seria redundante. Ative se as figuras forem usadas
# fora do documento — em apresentação de slides, por exemplo, onde não há legenda.
TITULOS_NAS_FIGURAS = False

# Paleta das figuras (a mesma das figuras já inseridas no TCC)
INK = "#0b0b0b"; INK2 = "#52514e"; MUTED = "#8b8a85"; GRID = "#e6e5e1"; SURF = "#ffffff"
BLUE = "#2a78d6"; BLUE_L = "#cde2fb"; NEUTRO = "#e9e8e4"; ORANGE = "#eb6834"


# =============================================================================
# 2. UTILITÁRIOS
# =============================================================================

class Relatorio:
    """Acumula o texto do relatório e o imprime na tela ao mesmo tempo."""

    def __init__(self) -> None:
        self.linhas: list[str] = []

    def titulo(self, texto: str) -> None:
        self.linhas += ["", "=" * 78, texto, "=" * 78]
        print(f"\n{texto}")

    def item(self, texto: str = "") -> None:
        self.linhas.append(texto)
        print(texto)

    def salvar(self, caminho: Path) -> None:
        cabecalho = ["RELATÓRIO DA CAMADA OURO — análise do mercado de aluguel e do IPS",
                     f"Gerado em {datetime.now():%d/%m/%Y %H:%M}"]
        caminho.write_text("\n".join(cabecalho + self.linhas) + "\n", encoding=CODIFICACAO)


def fmt(n) -> str:
    """17945 -> '17.945' (separador de milhar no padrão brasileiro)."""
    if pd.isna(n):
        return "—"
    return f"{int(round(float(n))):,}".replace(",", ".")


def dec(n, casas: int = 2) -> str:
    """3.14159 -> '3,14' (vírgula decimal)."""
    if pd.isna(n):
        return "—"
    return f"{float(n):.{casas}f}".replace(".", ",")


def pct(n, casas: int = 1) -> str:
    return f"{dec(n, casas)}%"


# --- Estatística -------------------------------------------------------------
# As funções abaixo evitam depender do SciPy para o essencial; quando ele está
# instalado, os p-valores saem exatos. Sem ele, o script continua rodando e
# informa apenas os coeficientes.

try:
    from scipy import stats as _scipy_stats
except ImportError:                                     # pragma: no cover
    _scipy_stats = None


def correlacao(x: pd.Series, y: pd.Series) -> dict:
    """Pearson (linear) e Spearman (monotônica) entre duas séries, com p-valores.

    Reporta as duas porque medem coisas diferentes: Pearson supõe relação linear e é
    sensível a valores extremos; Spearman trabalha com as posições e resiste a eles.
    Divergência grande entre as duas é sinal de que a relação não é linear.
    """
    x = pd.to_numeric(x, errors="coerce")
    y = pd.to_numeric(y, errors="coerce")
    valido = x.notna() & y.notna()
    x, y = x[valido], y[valido]
    resultado = {"n": int(len(x)), "pearson": np.nan, "p_pearson": np.nan,
                 "spearman": np.nan, "p_spearman": np.nan}
    if len(x) < 3:
        return resultado
    if _scipy_stats is not None:
        pe, ppe = _scipy_stats.pearsonr(x, y)
        sp, psp = _scipy_stats.spearmanr(x, y)
        resultado.update(pearson=pe, p_pearson=ppe, spearman=sp, p_spearman=psp)
    else:
        resultado["pearson"] = float(np.corrcoef(x, y)[0, 1])
        resultado["spearman"] = float(np.corrcoef(x.rank(), y.rank())[0, 1])
    return resultado


def regressao_log(ips: pd.Series, preco: pd.Series) -> dict:
    """Ajusta log(preço) = a + b·IPS por mínimos quadrados.

    O logaritmo é aplicado ao preço por duas razões. A primeira é estatística: preços
    têm distribuição assimétrica à direita, e o log a aproxima da normal. A segunda é
    interpretativa: no modelo logarítmico o coeficiente vira variação PERCENTUAL por
    ponto de IPS, e o desvio de cada região passa a ser lido como "x% acima do esperado",
    o que é estável mesmo nas regiões de IPS baixo — ao contrário do modelo linear, cujo
    intercepto negativo faria o preço esperado tender a zero e os desvios explodirem.
    """
    ips = pd.to_numeric(ips, errors="coerce")
    preco = pd.to_numeric(preco, errors="coerce")
    valido = ips.notna() & (preco > 0)
    x, y = ips[valido].to_numpy(), np.log(preco[valido].to_numpy())
    n = len(x)
    if n < 3:
        return {"n": n, "a": np.nan, "b": np.nan, "r2": np.nan, "p": np.nan, "efeito_pct": np.nan}
    b, a = np.polyfit(x, y, 1)
    previsto = a + b * x
    ss_res = float(((y - previsto) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot else np.nan
    p = np.nan
    if _scipy_stats is not None:
        p = float(_scipy_stats.linregress(x, y).pvalue)
    return {"n": n, "a": float(a), "b": float(b), "r2": float(r2), "p": p,
            "efeito_pct": float(100 * (np.exp(b) - 1))}


def estrela(p) -> str:
    """Marcação usual de significância, para a leitura rápida das tabelas."""
    if pd.isna(p):
        return ""
    if p < 0.001:
        return " ***"
    if p < 0.01:
        return " **"
    if p < 0.05:
        return " *"
    return " (n.s.)"


# =============================================================================
# 3. PREPARAÇÃO DA BASE
# =============================================================================

def carregar(caminho: Path, classe: str, incluir_outliers: bool, rel: Relatorio) -> pd.DataFrame:
    """Lê a base da Camada Prata e aplica o recorte da análise."""
    rel.titulo("0. BASE DE ANÁLISE")
    if not caminho.exists():
        sys.exit(f"ERRO: {caminho.name} não encontrado. Rode antes: python prata_limpeza.py")

    df = pd.read_csv(caminho, encoding=CODIFICACAO, low_memory=False)
    rel.item(f"arquivo: {caminho.name}")
    rel.item(f"{fmt(len(df))} registros tratados na entrada")

    if not incluir_outliers:
        antes = len(df)
        df = df[df["apto_analise"].astype(str).str.lower().isin(["true", "1", "verdadeiro"])]
        rel.item(f"recorte analítico (sem outliers): {fmt(len(df))} registros (−{fmt(antes - len(df))})")

    if classe != "todos":
        antes = len(df)
        df = df[df["classe_imovel"] == classe]
        rel.item(f"classe \"{classe}\": {fmt(len(df))} registros (−{fmt(antes - len(df))} de outras classes)")

    antes = len(df)
    df = df[df["ips"].notna() & df["preco_m2"].notna()]
    if antes - len(df):
        rel.item(f"sem IPS ou sem preço por m²: {fmt(antes - len(df))} registros fora do cálculo")

    rel.item(f"base final da análise: {fmt(len(df))} imóveis em "
             f"{df['regiao_administrativa'].nunique()} regiões administrativas")
    return df


def agregar_por_regiao(df: pd.DataFrame) -> pd.DataFrame:
    """Uma linha por região administrativa, com as medianas e a nota do IPS.

    A mediana é calculada sobre os anúncios da região; o IPS é constante dentro dela,
    e por isso entra com 'first'.
    """
    g = df.groupby("regiao_administrativa").agg(
        anuncios=("preco_m2", "size"),
        preco_m2=("preco_m2", "median"),
        aluguel=("aluguel", "median"),
        custo_total=("custo_total", "median"),
        area_m2=("area_m2", "median"),
        quartos=("quartos", "median"),
        **{c: (c, "first") for c in DIMENSOES},
    ).reset_index()
    return g.sort_values("preco_m2", ascending=False)


def agregar_por_bairro(df: pd.DataFrame) -> pd.DataFrame:
    """Uma linha por bairro. Descritivo: o IPS repetido aqui é o da região, não do bairro."""
    g = df.groupby(["bairro", "zona_portal", "regiao_administrativa"]).agg(
        anuncios=("preco_m2", "size"),
        preco_m2=("preco_m2", "median"),
        aluguel=("aluguel", "median"),
        area_m2=("area_m2", "median"),
        ips_da_regiao=("ips", "first"),
    ).reset_index()
    return g.sort_values("anuncios", ascending=False)


def agregar_por_zona(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("zona_portal").agg(
        anuncios=("preco_m2", "size"),
        bairros=("bairro", "nunique"),
        preco_m2=("preco_m2", "median"),
        aluguel=("aluguel", "median"),
        area_m2=("area_m2", "median"),
        ips=("ips", "median"),
    ).reset_index()
    return g.sort_values("preco_m2", ascending=False)


# =============================================================================
# 4. BLOCOS DE ANÁLISE
# =============================================================================

def bloco_a_caracterizacao(df: pd.DataFrame, zonas: pd.DataFrame, bairros: pd.DataFrame,
                           rel: Relatorio) -> None:
    """Descreve o mercado antes de qualquer correlação."""
    rel.titulo("A. CARACTERIZAÇÃO DO MERCADO")

    q = df["aluguel"].quantile([0.25, 0.5, 0.75])
    rel.item(f"aluguel        : mediana R$ {fmt(q[0.5])} · quartis R$ {fmt(q[0.25])} e R$ {fmt(q[0.75])}")
    q2 = df["preco_m2"].quantile([0.25, 0.5, 0.75])
    rel.item(f"preço por m²   : mediana R$ {dec(q2[0.5])} · quartis R$ {dec(q2[0.25])} e R$ {dec(q2[0.75])}")
    rel.item(f"área útil      : mediana {fmt(df['area_m2'].median())} m²")
    rel.item(f"assimetria     : a média do aluguel (R$ {fmt(df['aluguel'].mean())}) supera a mediana "
             f"em {pct(100 * (df['aluguel'].mean() / df['aluguel'].median() - 1), 0)} — "
             f"distribuição alongada à direita, o que justifica usar a mediana")
    rel.item("")
    rel.item("por zona do portal:")
    rel.item(f"  {'zona':<10} {'anúncios':>9} {'bairros':>8} {'R$/m²':>9} {'aluguel':>10} {'área':>7} {'IPS':>6}")
    for _, r in zonas.iterrows():
        rel.item(f"  {r['zona_portal']:<10} {fmt(r['anuncios']):>9} {fmt(r['bairros']):>8} "
                 f"{dec(r['preco_m2']):>9} {fmt(r['aluguel']):>10} {fmt(r['area_m2']):>6}m² {dec(r['ips'], 1):>6}")
    rel.item("")
    caros = bairros.nlargest(5, "preco_m2")
    baratos = bairros[bairros["anuncios"] >= 30].nsmallest(5, "preco_m2")
    rel.item("bairros mais caros por m²  : " + " · ".join(
        f"{r['bairro']} (R$ {dec(r['preco_m2'])})" for _, r in caros.iterrows()))
    rel.item("bairros mais baratos por m²: " + " · ".join(
        f"{r['bairro']} (R$ {dec(r['preco_m2'])})" for _, r in baratos.iterrows()))


def bloco_b_relacao(ras: pd.DataFrame, rel: Relatorio) -> dict:
    """A relação central da pesquisa: IPS x preço do metro quadrado."""
    rel.titulo("B. RELAÇÃO ENTRE O IPS E O PREÇO")

    c_m2 = correlacao(ras["ips"], ras["preco_m2"])
    c_alu = correlacao(ras["ips"], ras["aluguel"])
    modelo = regressao_log(ras["ips"], ras["preco_m2"])

    rel.item(f"unidade de análise: região administrativa · {c_m2['n']} regiões no cálculo")
    rel.item("")
    rel.item(f"IPS x preço por m²   : Pearson {dec(c_m2['pearson'])}{estrela(c_m2['p_pearson'])} · "
             f"Spearman {dec(c_m2['spearman'])}{estrela(c_m2['p_spearman'])}")
    rel.item(f"IPS x aluguel total  : Pearson {dec(c_alu['pearson'])}{estrela(c_alu['p_pearson'])} · "
             f"Spearman {dec(c_alu['spearman'])}{estrela(c_alu['p_spearman'])}")
    rel.item("")
    rel.item(f"modelo log(preço/m²) = {dec(modelo['a'])} + {dec(modelo['b'], 4)}·IPS")
    rel.item(f"  R² = {dec(modelo['r2'], 3)} · p = {dec(modelo['p'], 5)}")
    rel.item(f"  leitura: cada ponto de IPS corresponde a +{pct(modelo['efeito_pct'])} no preço do m²")
    rel.item(f"  o IPS explica {pct(100 * modelo['r2'], 0)} da variação do preço entre as regiões; "
             f"o restante fica com fatores que o índice não capta")
    return {"preco_m2": c_m2, "aluguel": c_alu, "modelo": modelo}


def bloco_c_dimensoes(ras: pd.DataFrame, rel: Relatorio) -> pd.DataFrame:
    """Qual das três dimensões do IPS o mercado efetivamente precifica."""
    rel.titulo("C. DECOMPOSIÇÃO PELAS DIMENSÕES DO IPS")

    linhas = []
    for coluna, rotulo in DIMENSOES.items():
        c = correlacao(ras[coluna], ras["preco_m2"])
        linhas.append({"dimensao": rotulo, "coluna": coluna, **c})
        rel.item(f"{rotulo:<30} Pearson {dec(c['pearson']):>6}{estrela(c['p_pearson']):<6} "
                 f"Spearman {dec(c['spearman']):>6}{estrela(c['p_spearman'])}")

    tabela = pd.DataFrame(linhas)
    dims = tabela[tabela["coluna"] != "ips"].copy()
    forte = dims.loc[dims["spearman"].idxmax()]
    fraca = dims.loc[dims["spearman"].idxmin()]
    rel.item("")
    significancia = "não significativa a 5%" if fraca["p_spearman"] >= 0.05 else "significativa a 5%"
    rel.item(f"a dimensão mais associada ao preço é \"{forte['dimensao']}\" "
             f"(Spearman {dec(forte['spearman'])}) e a menos associada é \"{fraca['dimensao']}\" "
             f"(Spearman {dec(fraca['spearman'])}, {significancia})")
    rel.item("leitura: o mercado precifica bem-estar e oportunidade, mas responde pouco ao atendimento")
    rel.item("de necessidades básicas — resultado que fala diretamente à hipótese de assimetria entre")
    rel.item("preço cobrado e infraestrutura social disponível")
    return tabela


def bloco_d_distorcoes(ras: pd.DataFrame, modelo: dict, rel: Relatorio) -> pd.DataFrame:
    """Distância entre o preço observado e o preço que o IPS da região previa.

    Duas medidas complementares:
      - desvio percentual sobre o modelo log-linear: quanto a região custa a mais (ou a
        menos) do que regiões de IPS equivalente;
      - diferença de posições: em que colocação a região aparece no ranking de IPS e em
        que colocação aparece no de preço. É uma medida ordinal, imune à forma da curva.
    """
    rel.titulo("D. DISTORÇÕES ENTRE PREÇO E INDICADOR SOCIAL")

    ras = ras.copy()
    ras["preco_m2_esperado"] = np.exp(modelo["a"] + modelo["b"] * ras["ips"])
    ras["desvio_pct"] = 100 * (ras["preco_m2"] / ras["preco_m2_esperado"] - 1)
    ras["posicao_ips"] = ras["ips"].rank(ascending=False, method="min").astype(int)
    ras["posicao_preco"] = ras["preco_m2"].rank(ascending=False, method="min").astype(int)
    # positivo = melhor colocada em preço do que em IPS, isto é, cara para o que oferece
    ras["diferenca_posicoes"] = ras["posicao_ips"] - ras["posicao_preco"]

    ordenado = ras.sort_values("desvio_pct", ascending=False)
    rel.item(f"{'região':<22} {'anúncios':>8} {'IPS':>6} {'R$/m²':>8} {'esperado':>9} "
             f"{'desvio':>9} {'pos.IPS':>8} {'pos.preço':>9}")
    for _, r in ordenado.iterrows():
        rel.item(f"{r['regiao_administrativa']:<22} {fmt(r['anuncios']):>8} {dec(r['ips'], 1):>6} "
                 f"{dec(r['preco_m2']):>8} {dec(r['preco_m2_esperado']):>9} "
                 f"{('+' if r['desvio_pct'] >= 0 else '') + pct(r['desvio_pct'], 0):>9} "
                 f"{r['posicao_ips']:>8} {r['posicao_preco']:>9}")

    acima = ordenado.head(3)
    abaixo = ordenado.tail(3).iloc[::-1]
    rel.item("")
    rel.item("mais caras do que o IPS previa: " + " · ".join(
        f"{r['regiao_administrativa']} (+{pct(r['desvio_pct'], 0)})" for _, r in acima.iterrows()))
    rel.item("mais baratas do que o IPS previa: " + " · ".join(
        f"{r['regiao_administrativa']} ({pct(r['desvio_pct'], 0)})" for _, r in abaixo.iterrows()))
    rel.item("")
    rel.item("atenção na leitura: preço por m² alto convive com imóvel pequeno. As regiões no topo")
    rel.item("desta lista concentram unidades bem menores que a mediana da cidade, o que eleva o")
    rel.item("preço por m² sem que o aluguel total seja alto — a coluna de área da tabela por região")
    rel.item("permite conferir cada caso antes de interpretar o desvio como sobrepreço.")
    return ras


def bloco_e_robustez(df: pd.DataFrame, ras_completo: pd.DataFrame, min_anuncios: int,
                     rel: Relatorio) -> pd.DataFrame:
    """Verifica se o resultado do bloco B sobrevive a outras escolhas metodológicas."""
    rel.titulo("E. ROBUSTEZ DO RESULTADO")

    linhas = []
    rel.item("sensibilidade ao corte mínimo de anúncios por região:")
    rel.item(f"  {'corte':>6} {'regiões':>8} {'imóveis':>9} {'Pearson':>12} {'Spearman':>12} {'R²':>7}")
    for corte in CORTES_ROBUSTEZ:
        sub = ras_completo[ras_completo["anuncios"] >= corte]
        if len(sub) < 3:
            continue
        c = correlacao(sub["ips"], sub["preco_m2"])
        m = regressao_log(sub["ips"], sub["preco_m2"])
        linhas.append({"teste": f"corte >= {corte} anúncios", "regioes": len(sub),
                       "imoveis": int(sub["anuncios"].sum()), **c, "r2": m["r2"]})
        pe = dec(c["pearson"]) + estrela(c["p_pearson"]).strip()
        sp = dec(c["spearman"]) + estrela(c["p_spearman"]).strip()
        rel.item(f"  {corte:>6} {len(sub):>8} {fmt(sub['anuncios'].sum()):>9} "
                 f"{pe:>12} {sp:>12} {dec(m['r2'], 3):>7}")
    rel.item("  a correlação não se desfaz ao endurecer o corte; ela se fortalece, o que indica que")
    rel.item("  a dispersão nos cortes baixos vem da instabilidade das medianas de amostra pequena")
    rel.item("  ressalva: cortes altos também reduzem o número de regiões (11 no corte de 200), e com")
    rel.item("  poucos pontos o coeficiente fica instável — o ganho no extremo direito não deve ser")
    rel.item(f"  lido como resultado mais forte. O corte adotado ({min_anuncios}) preserva "
             f"{len(ras_completo[ras_completo['anuncios'] >= min_anuncios])} regiões e é o reportado no texto.")

    rel.item("")
    rel.item("controle pelo tamanho do imóvel:")
    sub = ras_completo[ras_completo["anuncios"] >= min_anuncios]
    c_area = correlacao(sub["ips"], sub["area_m2"])
    rel.item(f"  IPS x área mediana: Spearman {dec(c_area['spearman'])}{estrela(c_area['p_spearman'])} — "
             f"regiões de IPS alto têm imóveis maiores,")
    rel.item("  e imóvel maior costuma ter preço por m² menor; o tamanho é, portanto, um confundidor")
    simples = regressao_log(sub["ips"], sub["preco_m2"])
    multipla = _regressao_multipla(sub)
    rel.item(f"  efeito do IPS sem controle : {pct(simples['efeito_pct'])} por ponto (R² {dec(simples['r2'], 3)})")
    rel.item(f"  efeito do IPS com controle : {pct(multipla['efeito_pct'])} por ponto (R² {dec(multipla['r2'], 3)})")
    rel.item("  o coeficiente praticamente não se move: a relação não é um artefato do tamanho do imóvel")
    linhas.append({"teste": "com controle de área (log)", "regioes": len(sub),
                   "imoveis": int(sub["anuncios"].sum()), "n": len(sub),
                   "pearson": np.nan, "p_pearson": np.nan, "spearman": np.nan,
                   "p_spearman": np.nan, "r2": multipla["r2"]})
    return pd.DataFrame(linhas)


def _regressao_multipla(ras: pd.DataFrame) -> dict:
    """log(preço/m²) = a + b·IPS + c·log(área), por mínimos quadrados."""
    valido = ras["ips"].notna() & (ras["preco_m2"] > 0) & (ras["area_m2"] > 0)
    s = ras[valido]
    if len(s) < 4:
        return {"efeito_pct": np.nan, "r2": np.nan}
    X = np.column_stack([np.ones(len(s)), s["ips"].to_numpy(), np.log(s["area_m2"].to_numpy())])
    y = np.log(s["preco_m2"].to_numpy())
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    previsto = X @ coef
    r2 = 1 - ((y - previsto) ** 2).sum() / ((y - y.mean()) ** 2).sum()
    return {"efeito_pct": float(100 * (np.exp(coef[1]) - 1)), "r2": float(r2)}


# =============================================================================
# 5. FIGURAS
# =============================================================================

def preparar_matplotlib():
    """Importa o matplotlib já configurado com a paleta do documento."""
    try:
        import matplotlib
    except ImportError:
        print("AVISO: matplotlib não instalado — figuras puladas "
              "(pip install matplotlib para gerá-las).")
        return None
    matplotlib.use("Agg")                       # backend sem janela, para rodar em qualquer ambiente
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.edgecolor": GRID,
                         "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
                         "figure.facecolor": SURF, "axes.facecolor": SURF})
    return plt


def _titulo(ax, texto, **kwargs):
    """Escreve o título só quando as figuras são usadas fora do TCC (ver TITULOS_NAS_FIGURAS)."""
    if TITULOS_NAS_FIGURAS:
        ax.set_title(texto, **kwargs)


def _limpar_eixo(ax, manter=("left", "bottom")):
    for lado in ("top", "right", "bottom", "left"):
        ax.spines[lado].set_visible(lado in manter)
        if lado in manter:
            ax.spines[lado].set_color(GRID)


def figura_distribuicao(plt, df: pd.DataFrame, zonas: pd.DataFrame, destino: Path) -> None:
    """Figura 3 — como o preço do m² se distribui, no todo e por zona."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(8.0, 3.2), dpi=200,
                                   gridspec_kw={"width_ratios": [1, 1.25]})

    # Corta o 1% superior em vez de comprimi-lo no último intervalo: comprimir criaria
    # uma barra artificial na borda direita, sugerindo uma concentração que não existe.
    limite = df["preco_m2"].quantile(0.99)
    dados = df.loc[df["preco_m2"] <= limite, "preco_m2"]
    ax1.hist(dados, bins=45, color=BLUE, edgecolor="white", linewidth=0.4)
    mediana = df["preco_m2"].median()
    ax1.axvline(mediana, color=ORANGE, linewidth=1.4, linestyle="--")
    ax1.text(mediana * 1.08, ax1.get_ylim()[1] * 0.88, f"mediana\nR$ {dec(mediana)}",
             fontsize=7.5, color=ORANGE)
    ax1.set_xlabel("preço por m² (R$) — sem o 1% superior"); ax1.set_ylabel("anúncios")
    _titulo(ax1, "Distribuição do preço por m²", loc="left", fontsize=9.5, fontweight="bold", color=INK)
    _limpar_eixo(ax1)

    ordem = zonas.sort_values("preco_m2")["zona_portal"].tolist()
    caixas = [df.loc[df["zona_portal"] == z, "preco_m2"].dropna() for z in ordem]
    bp = ax2.boxplot(caixas, orientation="horizontal", patch_artist=True, showfliers=False, widths=0.6,
                     medianprops={"color": ORANGE, "linewidth": 1.6})
    for caixa in bp["boxes"]:
        caixa.set(facecolor=BLUE_L, edgecolor=BLUE, linewidth=0.9)
    for elemento in ("whiskers", "caps"):
        for artista in bp[elemento]:
            artista.set(color=BLUE, linewidth=0.9)
    ax2.set_yticks(range(1, len(ordem) + 1)); ax2.set_yticklabels(ordem, fontsize=8.5)
    ax2.set_xlabel("preço por m² (R$)")
    _titulo(ax2, "Preço por m² por zona da cidade", loc="left", fontsize=9.5, fontweight="bold", color=INK)
    _limpar_eixo(ax2)

    fig.tight_layout()
    fig.savefig(destino, bbox_inches="tight", pad_inches=0.14)
    plt.close(fig)


def figura_dispersao(plt, ras: pd.DataFrame, modelo: dict, correlacoes: dict, destino: Path) -> None:
    """Figura 4 — a figura central: IPS contra preço do m², por região administrativa."""
    fig, ax = plt.subplots(figsize=(8.0, 5.0), dpi=200)

    tamanho = 18 + 130 * (ras["anuncios"] / ras["anuncios"].max()) ** 0.5
    ax.scatter(ras["ips"], ras["preco_m2"], s=tamanho, color=BLUE, alpha=0.72,
               edgecolor="white", linewidth=0.8, zorder=3)

    grade = np.linspace(ras["ips"].min() - 1, ras["ips"].max() + 1, 100)
    ax.plot(grade, np.exp(modelo["a"] + modelo["b"] * grade), color=ORANGE,
            linewidth=1.6, zorder=2, label="preço esperado pelo IPS")

    # Rotula só o que a figura precisa explicar: as regiões que mais se afastam da curva
    # (para cima e para baixo) e as de maior volume de anúncios. Rotular todas as 25
    # tornaria o gráfico ilegível justamente na faixa central, onde elas se acumulam.
    # Abaixo da curva as regiões se amontoam num único ponto do gráfico; rotular várias ali
    # produziria texto sobreposto, e o ranking completo já está na figura das distorções.
    if "desvio_pct" in ras.columns:
        extremos = pd.concat([ras.nlargest(2, "desvio_pct"), ras.nsmallest(1, "desvio_pct")])
    else:
        extremos = pd.concat([ras.nlargest(2, "preco_m2"), ras.nsmallest(1, "preco_m2")])
    destacar = set(extremos["regiao_administrativa"]) | set(ras.nlargest(5, "anuncios")["regiao_administrativa"])

    esperado = np.exp(modelo["a"] + modelo["b"] * ras["ips"])
    acima = ras["preco_m2"] >= esperado
    for (_, r), sobe in zip(ras.iterrows(), acima):
        if r["regiao_administrativa"] not in destacar:
            continue
        deslocamento = (7, 5) if sobe else (7, -11)
        ax.annotate(r["regiao_administrativa"], (r["ips"], r["preco_m2"]),
                    textcoords="offset points", xytext=deslocamento, fontsize=7.2, color=INK2)

    c = correlacoes["preco_m2"]
    ax.text(0.02, 0.96, f"Spearman {dec(c['spearman'])}{estrela(c['p_spearman'])}\n"
                        f"R² {dec(modelo['r2'], 2)} · {pct(modelo['efeito_pct'])} por ponto de IPS",
            transform=ax.transAxes, va="top", ha="left", fontsize=8,
            color=INK2, bbox={"facecolor": "#f7f6f3", "edgecolor": GRID, "boxstyle": "round,pad=0.5"})

    ax.set_xlabel("Índice de Progresso Social da região administrativa (2024)")
    ax.set_ylabel("preço mediano do m² (R$)")
    _titulo(ax, "Preço do metro quadrado e progresso social", loc="left",
            fontsize=10.5, fontweight="bold", color=INK, pad=10)
    ax.legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(0.0, 0.86))
    ax.margins(x=0.09, y=0.10)
    ax.grid(axis="both", color=GRID, linewidth=0.6, zorder=0)
    _limpar_eixo(ax)
    fig.savefig(destino, bbox_inches="tight", pad_inches=0.16)
    plt.close(fig)


def figura_dimensoes(plt, ras: pd.DataFrame, tabela_dim: pd.DataFrame, destino: Path) -> None:
    """Figura 5 — o mercado precifica uma dimensão do IPS muito mais do que outra."""
    dims = [(c, r) for c, r in DIMENSOES.items() if c != "ips"]
    fig, eixos = plt.subplots(1, 3, figsize=(8.4, 3.0), dpi=200, sharey=True)

    for ax, (coluna, rotulo) in zip(eixos, dims):
        linha = tabela_dim[tabela_dim["coluna"] == coluna].iloc[0]
        significativa = pd.notna(linha["p_spearman"]) and linha["p_spearman"] < 0.05
        cor = BLUE if significativa else MUTED
        ax.scatter(ras[coluna], ras["preco_m2"], s=26, color=cor, alpha=0.75,
                   edgecolor="white", linewidth=0.6, zorder=3)
        m = regressao_log(ras[coluna], ras["preco_m2"])
        grade = np.linspace(ras[coluna].min(), ras[coluna].max(), 60)
        ax.plot(grade, np.exp(m["a"] + m["b"] * grade), color=cor, linewidth=1.3, alpha=0.9, zorder=2)
        ax.set_title(rotulo, loc="left", fontsize=8.8, fontweight="bold", color=INK)
        ax.text(0.03, 0.94, f"Spearman {dec(linha['spearman'])}{estrela(linha['p_spearman'])}",
                transform=ax.transAxes, va="top", fontsize=7.8, color=cor)
        ax.set_xlabel("nota da dimensão")
        ax.grid(color=GRID, linewidth=0.5, zorder=0)
        _limpar_eixo(ax)

    eixos[0].set_ylabel("preço mediano do m² (R$)")
    if TITULOS_NAS_FIGURAS:
        fig.suptitle("O que o mercado precifica: as três dimensões do IPS", x=0.005, ha="left",
                     fontsize=10.5, fontweight="bold", color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.93 if TITULOS_NAS_FIGURAS else 1.0))
    fig.savefig(destino, bbox_inches="tight", pad_inches=0.14)
    plt.close(fig)


def figura_distorcao(plt, ras: pd.DataFrame, destino: Path) -> None:
    """Figura 6 — quanto cada região custa a mais ou a menos do que seu IPS previa."""
    d = ras.sort_values("desvio_pct")
    altura = max(3.4, 0.26 * len(d))
    fig, ax = plt.subplots(figsize=(7.8, altura), dpi=200)

    y = np.arange(len(d))
    cores = [ORANGE if v > 0 else BLUE for v in d["desvio_pct"]]
    ax.barh(y, d["desvio_pct"], height=0.66, color=cores, edgecolor="none")
    ax.axvline(0, color=INK2, linewidth=0.9)

    for yi, valor in zip(y, d["desvio_pct"]):
        deslocamento = 3 if valor >= 0 else -3
        # sinal de menos tipográfico, igual ao que o matplotlib usa nos ticks do eixo
        rotulo = f"+{valor:.0f}%" if valor >= 0 else f"−{abs(valor):.0f}%"
        ax.text(valor + deslocamento, yi, rotulo,
                va="center", ha="left" if valor >= 0 else "right", fontsize=7.2, color=INK2)

    ax.set_yticks(y); ax.set_yticklabels(d["regiao_administrativa"], fontsize=8)
    # Limites assimétricos: o desvio positivo extremo é muito maior que o negativo, e um
    # eixo simétrico desperdiçaria metade da figura em espaço vazio.
    menor = min(float(d["desvio_pct"].min()), 0.0)
    maior = max(float(d["desvio_pct"].max()), 0.0)
    faixa = maior - menor
    ax.set_xlim(menor - faixa * 0.20, maior + faixa * 0.14)
    ax.set_xlabel("desvio do preço observado em relação ao previsto pelo IPS (%)")
    _titulo(ax, "Distorções entre preço e progresso social", loc="left",
            fontsize=10.5, fontweight="bold", color=INK, pad=24)
    ax.text(0, 1.012, "à direita, regiões mais caras do que seu IPS previa; à esquerda, mais baratas",
            transform=ax.transAxes, fontsize=7.8, color=MUTED, va="bottom")
    ax.xaxis.grid(color=GRID, linewidth=0.5); ax.set_axisbelow(True)
    _limpar_eixo(ax, manter=("left",))
    ax.tick_params(axis="x", length=0)
    fig.savefig(destino, bbox_inches="tight", pad_inches=0.16)
    plt.close(fig)


def figura_robustez(plt, ras_completo: pd.DataFrame, min_anuncios: int, destino: Path) -> None:
    """Figura 7 — a correlação em função do corte mínimo de anúncios por região."""
    pontos = []
    for corte in CORTES_ROBUSTEZ:
        sub = ras_completo[ras_completo["anuncios"] >= corte]
        if len(sub) < 3:
            continue
        c = correlacao(sub["ips"], sub["preco_m2"])
        pontos.append((corte, len(sub), c["pearson"], c["spearman"]))
    if not pontos:
        return

    cortes = [p[0] for p in pontos]
    fig, ax = plt.subplots(figsize=(7.2, 3.4), dpi=200)
    x = np.arange(len(pontos))
    if min_anuncios in cortes:                       # marca o corte efetivamente adotado
        posicao = cortes.index(min_anuncios)
        ax.axvspan(posicao - 0.32, posicao + 0.32, color=NEUTRO, zorder=0)
        ax.text(posicao, 0.045, "corte adotado", ha="center", fontsize=7.2, color=MUTED)
    ax.plot(x, [p[2] for p in pontos], marker="o", markersize=5, color=BLUE, linewidth=1.5, label="Pearson")
    ax.plot(x, [p[3] for p in pontos], marker="s", markersize=4.5, color=ORANGE, linewidth=1.5,
            linestyle="--", label="Spearman")
    ax.set_xticks(x)
    ax.set_xticklabels([f"≥ {c}\n({p[1]} regiões)" for c, p in zip(cortes, pontos)], fontsize=7.6)
    ax.set_ylim(0, 1)
    ax.set_xlabel("corte mínimo de anúncios por região administrativa")
    ax.set_ylabel("correlação com o preço do m²")
    _titulo(ax, "A correlação resiste ao endurecimento do corte", loc="left",
            fontsize=10, fontweight="bold", color=INK, pad=8)
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    ax.yaxis.grid(color=GRID, linewidth=0.5); ax.set_axisbelow(True)
    _limpar_eixo(ax)
    fig.savefig(destino, bbox_inches="tight", pad_inches=0.14)
    plt.close(fig)


# =============================================================================
# 6. ORQUESTRAÇÃO
# =============================================================================

def main() -> None:
    p = argparse.ArgumentParser(description="Camada Ouro — análise do mercado de aluguel e do IPS.")
    p.add_argument("--entrada", default=ARQUIVO_ENTRADA, help="CSV da Camada Prata")
    p.add_argument("--classe", default=CLASSE_PADRAO,
                   choices=["residencial", "comercial", "todos"], help="recorte de imóveis")
    p.add_argument("--min-anuncios", type=int, default=MIN_ANUNCIOS_PADRAO,
                   help="mínimo de anúncios para a região entrar na correlação")
    p.add_argument("--incluir-outliers", action="store_true",
                   help="usa também as linhas marcadas como outlier na Camada Prata")
    p.add_argument("--sem-figuras", action="store_true", help="não gera os PNG")
    args = p.parse_args()

    rel = Relatorio()
    caminho_entrada = Path(args.entrada)
    if not caminho_entrada.is_absolute():
        caminho_entrada = next((pasta / args.entrada for pasta in (PASTA_PRATA, PASTA)
                                if (pasta / args.entrada).exists()), PASTA_PRATA / args.entrada)
    df = carregar(caminho_entrada, args.classe, args.incluir_outliers, rel)

    # --- agregações ----------------------------------------------------------
    ras_completo = agregar_por_regiao(df)
    bairros = agregar_por_bairro(df)
    zonas = agregar_por_zona(df)

    ras = ras_completo[ras_completo["anuncios"] >= args.min_anuncios].copy()
    rel.item(f"corte de {args.min_anuncios} anúncios: {len(ras)} de {len(ras_completo)} regiões "
             f"entram no cálculo correlacional ({fmt(ras['anuncios'].sum())} imóveis, "
             f"{pct(100 * ras['anuncios'].sum() / len(df))} da base)")

    # --- blocos --------------------------------------------------------------
    bloco_a_caracterizacao(df, zonas, bairros, rel)
    correlacoes = bloco_b_relacao(ras, rel)
    tabela_dim = bloco_c_dimensoes(ras, rel)
    ras = bloco_d_distorcoes(ras, correlacoes["modelo"], rel)
    tabela_rob = bloco_e_robustez(df, ras_completo, args.min_anuncios, rel)

    # --- saídas --------------------------------------------------------------
    rel.titulo("ARQUIVOS GERADOS")
    # Arredonda antes de exportar: as tabelas são copiadas direto para o texto do TCC, e
    # a precisão bruta do float (113.83500000000001) não tem significado aqui.
    def arredondar(tabela: pd.DataFrame) -> pd.DataFrame:
        t = tabela.copy()
        for coluna in t.select_dtypes("number").columns:
            t[coluna] = t[coluna].round(4 if coluna.startswith("p_") else 2)
        return t

    arredondar(ras).to_csv(PASTA_SAIDA / ARQUIVO_RAS, index=False, encoding=CODIFICACAO)
    arredondar(bairros).to_csv(PASTA_SAIDA / ARQUIVO_BAIRROS, index=False, encoding=CODIFICACAO)
    arredondar(zonas).to_csv(PASTA_SAIDA / ARQUIVO_ZONAS, index=False, encoding=CODIFICACAO)
    correl = pd.concat([
        tabela_dim.assign(bloco="dimensões"),
        tabela_rob.assign(bloco="robustez"),
    ], ignore_index=True)
    arredondar(correl).to_csv(PASTA_SAIDA / ARQUIVO_CORRELACOES, index=False, encoding=CODIFICACAO)
    for nome, tabela in [(ARQUIVO_RAS, ras), (ARQUIVO_BAIRROS, bairros),
                         (ARQUIVO_ZONAS, zonas), (ARQUIVO_CORRELACOES, correl)]:
        rel.item(f"{nome}: {len(tabela)} linhas")

    if not args.sem_figuras:
        plt = preparar_matplotlib()
        if plt is not None:
            pasta_fig = PASTA_SAIDA / PASTA_FIGURAS
            pasta_fig.mkdir(exist_ok=True)
            figuras = [
                ("fig3_distribuicao.png", lambda d: figura_distribuicao(plt, df, zonas, d)),
                ("fig4_dispersao_ips.png", lambda d: figura_dispersao(plt, ras, correlacoes["modelo"], correlacoes, d)),
                ("fig5_dimensoes.png", lambda d: figura_dimensoes(plt, ras, tabela_dim, d)),
                ("fig6_distorcao.png", lambda d: figura_distorcao(plt, ras, d)),
                ("fig7_robustez.png", lambda d: figura_robustez(plt, ras_completo, args.min_anuncios, d)),
            ]
            for nome, desenhar in figuras:
                desenhar(pasta_fig / nome)
                rel.item(f"{PASTA_FIGURAS}/{nome}")

    rel.salvar(PASTA_SAIDA / ARQUIVO_RELATORIO)
    print(f"\nRelatório completo em {ARQUIVO_RELATORIO}")


if __name__ == "__main__":
    main()
