# -*- coding: utf-8 -*-
"""
prata_limpeza.py — Camada Prata: tratamento e higienização da base bruta do ZAP Imóveis
=======================================================================================

Lê o CSV bruto da Camada Bronze (zap_imoveis_raw_rj.csv) e entrega uma base tipada,
deduplicada e pronta para o cruzamento com o IPS. Corresponde à Etapa 3 do cronograma
do TCC: remoção de duplicatas, conversão de moedas e normalização de textos.

O que o script faz, nesta ordem:
    1. Normaliza textos       — separa o bairro de "Bairro, Cidade", limpa espaços e acentos da chave
    2. Filtra o município     — descarta anúncios de fora do Rio de Janeiro
    3. Converte os números    — aluguel, condomínio, IPTU, área e quartos viram números
    4. Deriva atributos       — tipo de imóvel, classe (residencial/comercial), preço por m²
    5. Remove duplicatas      — mesmo imóvel anunciado mais de uma vez (ids diferentes)
    6. Marca outliers         — valores implausíveis ficam SINALIZADOS, não apagados
    7. Prepara o IPS          — chave de junção e, se os arquivos existirem, o de-para e o merge

Nada é apagado silenciosamente: cada linha descartada vai para um arquivo de auditoria e
cada etapa é contabilizada no relatório (relatorio_prata.txt).

Uso:
    python prata_limpeza.py                      # execução padrão
    python prata_limpeza.py --so-residencial     # base analítica só com imóveis residenciais
    python prata_limpeza.py --manter-duplicatas  # não remove duplicatas de conteúdo
    python prata_limpeza.py --entrada outro.csv --saida outro_prata.csv
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
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

PASTA_ENTRADA = pasta_do_projeto("0_entrada")   # insumos externos (quadro amostral, de-para, IPS)
PASTA_BRONZE = pasta_do_projeto("1_bronze")     # de onde vem a base bruta
PASTA_SAIDA = PASTA                             # esta camada escreve na própria pasta

ARQUIVO_ENTRADA = "zap_imoveis_raw_rj.csv"
ARQUIVO_SAIDA = "zap_imoveis_prata.csv"          # base tratada completa (com as marcações)
ARQUIVO_ANALISE = "zap_imoveis_prata_analise.csv"  # só as linhas aptas à análise
ARQUIVO_DESCARTES = "prata_descartes.csv"        # tudo que saiu da base, com o motivo
ARQUIVO_BAIRROS = "bairros_para_depara.csv"      # lista de bairros para montar o de-para do IPS
ARQUIVO_RELATORIO = "relatorio_prata.txt"
CODIFICACAO = "utf-8-sig"                        # com BOM: o Excel abre os acentos corretamente

MUNICIPIO_ALVO = "Rio de Janeiro"                # anúncios de outros municípios são descartados

# --- Arquivos do IPS (opcionais; o merge só acontece se forem encontrados) ----
# O script aceita qualquer um destes nomes, para não depender de como os arquivos
# forem salvos. Basta colocá-los na mesma pasta.
NOMES_DEPARA = ["depara_bairros_ra.xlsx", "depara_bairros_ra.csv", "depara_bairro_ra.csv",
                "de_para_bairros.csv", "depara.csv", "depara.xlsx"]
NOMES_IPS = ["ips_rio.xlsx", "ips_rio.csv", "ips_rio_2024.xlsx", "ips_rio_2024.csv", "ips.csv", "ips.xlsx"]
COLS_LISTA_BAIRROS = ["bairros", "bairro", "bairros da ra", "lista de bairros"]
# Rótulos usados pela planilha do IPS do Data.Rio (aba "Dimensões e Componentes")
ABA_IPS = "dimensoes e componentes"
ROTULO_RA_IPS = "regioes administrativas"
ROTULOS_DIMENSAO = {
    "indice de progresso social": "ips",
    "necessidades humanas basicas": "ips_necessidades_basicas",
    "fundamentos do bem estar": "ips_bem_estar",
    "oportunidades": "ips_oportunidades",
}
# Nomes de coluna aceitos em cada arquivo (o script procura o primeiro que existir)
COLS_BAIRRO = ["bairro", "nome_bairro", "bairro_zap", "nome"]
COLS_RA = ["regiao_administrativa", "ra", "regiao", "região administrativa", "zona_administrativa", "nome_ra"]
COLS_IPS = ["ips", "ips_geral", "ips geral", "nota_ips", "nota", "indice", "índice", "pontuacao", "score",
            "indice de progresso social", "ips_2024"]

# --- Textos que representam ausência ou isenção nos campos de valor -----------
TEXTO_ISENTO = "isento"
TEXTO_NAO_INFORMADO = "não informado"
# Etiquetas promocionais que o portal cola ao valor do IPTU nos registros de 06/09/2026
ETIQUETAS_PRECO = ["Ótimo preço", "Baixou de preço", "Aluguel sem fiador"]

# --- Sub-bairros que o portal trata como bairro, mas que não existem na divisão
# oficial do município (e, portanto, não têm IPS próprio). São reagrupados no
# bairro oficial correspondente. Ajuste esta tabela se o de-para do IPS pedir
# outro agrupamento.
AGRUPAR_BAIRRO = {
    "Arpoador": "Ipanema",
    "Barra Olímpica": "Barra da Tijuca",
    "Jardim Oceânico": "Barra da Tijuca",
    "Península": "Barra da Tijuca",
    "Castelo": "Centro",
    "Braz de Pina": "Brás de Pina",          # grafia alternativa usada pelo portal
    "Freguesia": "Freguesia (Jacarepaguá)",  # desambiguação: a outra Freguesia é da Ilha
}

# --- Tipos de imóvel, inferidos do início do slug da URL do anúncio -----------
# (o card não expõe o tipo como campo próprio)
TIPOS_IMOVEL = [
    ("apartamento", "Apartamento", "residencial"),
    ("casa-de-condominio", "Casa de condomínio", "residencial"),
    ("casa-de-vila", "Casa de vila", "residencial"),
    ("casa", "Casa", "residencial"),
    ("sobrado", "Sobrado", "residencial"),
    ("cobertura", "Cobertura", "residencial"),
    ("flat", "Flat", "residencial"),
    ("quitinete", "Kitnet/Quitinete", "residencial"),
    ("kitnet", "Kitnet/Quitinete", "residencial"),
    ("studio", "Studio/Loft", "residencial"),
    ("loft", "Studio/Loft", "residencial"),
    ("conjunto-comercial", "Sala/Conjunto comercial", "comercial"),
    ("sala", "Sala/Conjunto comercial", "comercial"),
    ("loja", "Loja/Salão/Ponto comercial", "comercial"),
    ("galpao", "Galpão/Depósito/Armazém", "comercial"),
    ("predio", "Prédio inteiro", "comercial"),
    ("andar", "Andar/Laje corporativa", "comercial"),
    ("terreno", "Terreno/Lote", "comercial"),
    ("hotel", "Hotel/Pousada", "comercial"),
    ("box", "Box/Garagem", "comercial"),
    ("garagem", "Box/Garagem", "comercial"),
    ("fazenda", "Fazenda/Sítio/Chácara", "rural"),
]

# --- Regras de plausibilidade (marcam outliers; NÃO apagam linhas) -----------
# Os limites foram calibrados sobre a distribuição observada na base de 06-07/09/2026,
# em que o preço por m² tem mediana de R$ 42, 1º percentil de R$ 6 e 99º de R$ 369.
LIMITES = {
    "aluguel_min": 300,        # abaixo disso, quase sempre erro de cadastro ou taxa avulsa
    "aluguel_max": 1_000_000,  # acima disso, erro de digitação (ex.: R$ 15.000.000)
    "area_min": 10,            # m²
    "area_max": 10_000,        # m² (galpões grandes são legítimos; acima disso, erro)
    "preco_m2_min": 5,         # R$/m² por mês
    "preco_m2_max": 500,       # R$/m² por mês
    "condominio_sobre_aluguel_max": 3,   # condomínio acima de 3x o aluguel é suspeito
}

# --- Deduplicação de conteúdo ------------------------------------------------
# A Camada Bronze já removeu repetições do MESMO anúncio (id igual). Aqui o alvo é
# outro: o mesmo IMÓVEL publicado por anunciantes diferentes ou republicado, o que
# gera ids distintos e inflaria a média do bairro. Dois registros são considerados
# o mesmo imóvel quando coincidem em todos os campos abaixo.
CHAVE_DUPLICATA = ["bairro", "rua", "aluguel", "condominio", "area_m2", "quartos"]

# Ordem final das colunas na base tratada
COLUNAS_SAIDA = [
    # identificação e localização
    "id_anuncio", "bairro", "bairro_chave", "rua", "zona_portal", "bairro_busca",
    # atributos numéricos (o núcleo da análise)
    "aluguel", "condominio", "iptu", "custo_total", "area_m2", "quartos", "preco_m2",
    # classificação
    "tipo_imovel", "classe_imovel",
    # marcações de qualidade
    "area_estimada", "condominio_informado", "iptu_informado",
    "n_anuncios_iguais", "outlier", "motivo_outlier", "apto_analise",
    # rastreabilidade
    "etiqueta_preco", "tag_destaque", "url_anuncio", "pagina", "data_coleta",
]

# =============================================================================
# 2. UTILITÁRIOS DE NORMALIZAÇÃO
# =============================================================================

def normalizar_chave(texto: str) -> str:
    """Reduz um nome a uma chave comparável: sem acentos, sem maiúsculas, sem pontuação.

    "São Cristóvão" e "SAO CRISTOVAO " viram "sao cristovao", o que permite cruzar
    o bairro do anúncio com o de-para e com a base do IPS sem depender da grafia.
    """
    if not isinstance(texto, str):
        return ""
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    texto = re.sub(r"[^a-z0-9\s]", " ", texto.lower())   # remove parênteses, hífens, pontos e sublinhados
    return re.sub(r"\s+", " ", texto).strip()


def limpar_texto(texto: str) -> str:
    """Espaços repetidos, espaços não separáveis e sobras nas pontas."""
    if not isinstance(texto, str):
        return ""
    return re.sub(r"\s+", " ", texto.replace("\xa0", " ")).strip()


def para_numero(valor: str) -> float:
    """Converte "R$ 3.800" em 3800.0, "isento" em 0.0 e "não informado" em NaN.

    A distinção entre isento e não informado é essencial: tratar as duas como zero
    subestimaria o custo médio, e tratar as duas como ausente descartaria informação
    verdadeira (condomínio isento é um dado, não uma lacuna).
    """
    if not isinstance(valor, str) or not valor.strip():
        return np.nan
    texto = limpar_texto(valor)
    for etiqueta in ETIQUETAS_PRECO:                 # remove a etiqueta colada ao valor
        texto = texto.replace(etiqueta, "").strip()
    if texto.lower().startswith(TEXTO_ISENTO):
        return 0.0
    if texto.lower().startswith(TEXTO_NAO_INFORMADO):
        return np.nan
    numeros = re.sub(r"[^\d,]", "", texto).replace(",", ".")
    if not numeros or numeros.count(".") > 1:
        return np.nan
    try:
        return float(numeros)
    except ValueError:
        return np.nan


def extrair_etiqueta(valor: str) -> str:
    """Devolve a etiqueta promocional que o portal exibe junto ao preço, se houver."""
    if not isinstance(valor, str):
        return ""
    for etiqueta in ETIQUETAS_PRECO:
        if etiqueta.lower() in valor.lower():
            return etiqueta
    return ""


def area_para_numero(valor: str) -> tuple[float, bool]:
    """Converte "36 m²" em 36.0 e a faixa "130 - 135 m²" no ponto médio (132.5, estimada)."""
    if not isinstance(valor, str) or not valor.strip():
        return np.nan, False
    numeros = [float(x) for x in re.findall(r"\d+", valor.replace(".", ""))]
    if not numeros:
        return np.nan, False
    if len(numeros) >= 2 and "-" in valor:
        return (numeros[0] + numeros[1]) / 2, True
    return numeros[0], False


def classificar_imovel(url: str) -> tuple[str, str]:
    """Tipo e classe do imóvel, a partir do início do slug da URL do anúncio."""
    if not isinstance(url, str) or not url:
        return "Não identificado", "não identificado"
    casamento = re.search(r"/imovel/aluguel-([a-z0-9-]+)", url)
    slug = casamento.group(1) if casamento else ""
    for prefixo, rotulo, classe in TIPOS_IMOVEL:
        if slug.startswith(prefixo):
            return rotulo, classe
    return "Não identificado", "não identificado"


# =============================================================================
# 3. ETAPAS DO TRATAMENTO
# =============================================================================

class Relatorio:
    """Acumula as contagens de cada etapa para o arquivo de evidência."""

    def __init__(self) -> None:
        self.linhas: list[str] = []

    def titulo(self, texto: str) -> None:
        self.linhas += ["", "=" * 78, texto.upper(), "=" * 78]
        print(f"\n{texto}")

    def item(self, texto: str) -> None:
        self.linhas.append(texto)
        print(f"  {texto}")

    def salvar(self, caminho: Path) -> None:
        cabecalho = [
            "RELATÓRIO DA CAMADA PRATA — tratamento da base do ZAP Imóveis",
            f"Gerado em {datetime.now():%d/%m/%Y %H:%M}",
        ]
        caminho.write_text("\n".join(cabecalho + self.linhas) + "\n", encoding="utf-8")


def normalizar_textos(df: pd.DataFrame, rel: Relatorio) -> pd.DataFrame:
    """Etapa 1 — separa bairro e município, limpa textos e cria a chave de junção."""
    rel.titulo("1. Normalização de textos")

    # "Copacabana, Rio de Janeiro" → bairro "Copacabana" + município "Rio de Janeiro"
    partes = df["bairro"].fillna("").str.split(",", n=1, expand=True)
    df["bairro"] = partes[0].map(limpar_texto)
    df["municipio"] = partes[1].map(limpar_texto) if partes.shape[1] > 1 else MUNICIPIO_ALVO
    rel.item(f"bairro separado do município em {len(df)} registros")

    # Reagrupa sub-bairros que não existem na divisão oficial
    antes = df["bairro"].value_counts()
    df["bairro"] = df["bairro"].replace(AGRUPAR_BAIRRO)
    for origem, destino in AGRUPAR_BAIRRO.items():
        if origem in antes:
            rel.item(f"{antes[origem]:>5} anúncios de \"{origem}\" reagrupados em \"{destino}\"")

    df["rua"] = df["rua"].map(limpar_texto).replace("Endereço não informado", "")
    df["bairro_chave"] = df["bairro"].map(normalizar_chave)

    # Zona do portal (Central/Norte/Oeste/Sul): vem do quadro amostral, pelo bairro pesquisado
    quadro = PASTA_ENTRADA / "bairros_rj.csv"
    if quadro.exists():
        zonas = pd.read_csv(quadro, dtype=str, encoding=CODIFICACAO, keep_default_na=False)
        mapa = dict(zip(zonas["nome"], zonas["zona"].str.replace("zona-", "", regex=False).str.title()))
        df["zona_portal"] = df["bairro_busca"].map(mapa).fillna("")
        rel.item(f"zona do portal atribuída a {int((df['zona_portal'] != '').sum())} registros (via bairros_rj.csv)")
    else:
        df["zona_portal"] = ""
    rel.item(f"{df['bairro'].nunique()} bairros distintos após a normalização")
    rel.item(f"chave de junção criada (bairro_chave), ex.: \"São Cristóvão\" → \"{normalizar_chave('São Cristóvão')}\"")
    return df


def filtrar_municipio(df: pd.DataFrame, descartes: list[pd.DataFrame], rel: Relatorio) -> pd.DataFrame:
    """Etapa 2 — remove anúncios de fora do município do Rio de Janeiro."""
    rel.titulo("2. Filtro do município")
    fora = df[df["municipio"].map(normalizar_chave) != normalizar_chave(MUNICIPIO_ALVO)]
    if len(fora):
        for _, linha in fora.iterrows():
            rel.item(f"descartado: {linha['bairro']}, {linha['municipio']} (busca: {linha['bairro_busca']})")
        descartes.append(fora.assign(motivo_descarte="fora do município do Rio de Janeiro"))
    rel.item(f"{len(fora)} anúncio(s) de outros municípios descartado(s); restam {len(df) - len(fora)}")
    return df[~df.index.isin(fora.index)].copy()


def converter_numeros(df: pd.DataFrame, rel: Relatorio) -> pd.DataFrame:
    """Etapa 3 — moedas, área e quartos viram números."""
    rel.titulo("3. Conversão de moedas e medidas")

    # A etiqueta promocional vem em coluna própria nos registros coletados com o parser
    # corrigido e colada ao IPTU nos anteriores: as duas origens são unificadas aqui.
    da_coluna = df["etiqueta_preco_orig"].map(limpar_texto) if "etiqueta_preco_orig" in df.columns else ""
    do_iptu = df["valor_iptu"].map(extrair_etiqueta)
    df["etiqueta_preco"] = do_iptu.where(do_iptu != "", da_coluna)
    rel.item(f"etiqueta de preço recuperada em {int((df['etiqueta_preco'] != '').sum())} registros "
             f"({int((do_iptu != '').sum())} vindas coladas ao IPTU)")
    df["aluguel"] = df["valor_aluguel"].map(para_numero)
    df["condominio"] = df["valor_condominio"].map(para_numero)
    df["iptu"] = df["valor_iptu"].map(para_numero)

    for coluna, origem in [("aluguel", "valor_aluguel"), ("condominio", "valor_condominio"), ("iptu", "valor_iptu")]:
        isentos = int(df[origem].str.strip().str.lower().str.startswith(TEXTO_ISENTO).sum())
        ausentes = int(df[coluna].isna().sum())
        rel.item(f"{coluna:<11}: {len(df) - ausentes:>6} valores numéricos · {isentos:>5} isentos (0) · {ausentes:>5} ausentes")

    area = df["area_util"].map(area_para_numero)
    df["area_m2"] = [a for a, _ in area]
    df["area_estimada"] = [e for _, e in area]
    rel.item(f"area_m2    : {int(df['area_m2'].notna().sum()):>6} convertidas · {int(df['area_estimada'].sum()):>5} vindas de faixa (ponto médio)")

    df["quartos"] = pd.to_numeric(df["quartos"], errors="coerce").astype("Int64")
    rel.item(f"quartos    : {int(df['quartos'].notna().sum()):>6} informados · {int(df['quartos'].isna().sum()):>5} vazios (kitnets e imóveis comerciais)")

    # Marcações que preservam a diferença entre "isento" e "não informado"
    df["condominio_informado"] = ~df["condominio"].isna()
    df["iptu_informado"] = ~df["iptu"].isna()
    return df


def derivar_atributos(df: pd.DataFrame, rel: Relatorio) -> pd.DataFrame:
    """Etapa 4 — tipo de imóvel, custo total e preço por metro quadrado."""
    rel.titulo("4. Atributos derivados")

    tipos = df["url_anuncio"].map(classificar_imovel)
    df["tipo_imovel"] = [t for t, _ in tipos]
    df["classe_imovel"] = [c for _, c in tipos]
    for classe, n in df["classe_imovel"].value_counts().items():
        rel.item(f"classe {classe:<18}: {n:>6} ({n / len(df):.1%})")

    df["custo_total"] = (df["aluguel"] + df["condominio"].fillna(0)).round(2)
    df["preco_m2"] = np.round(np.where(df["area_m2"] > 0, df["aluguel"] / df["area_m2"], np.nan), 2)
    validos = df["preco_m2"].notna()
    rel.item(f"preco_m2 calculado em {int(validos.sum())} registros · mediana R$ {df.loc[validos, 'preco_m2'].median():.2f}/m²")
    return df


def remover_duplicatas(df: pd.DataFrame, descartes: list[pd.DataFrame], rel: Relatorio,
                       manter: bool = False) -> pd.DataFrame:
    """Etapa 5 — um registro por imóvel."""
    rel.titulo("5. Remoção de duplicatas")

    repetidos_id = int(df.loc[df["id_anuncio"] != "", "id_anuncio"].duplicated().sum())
    rel.item(f"anúncios com id repetido (já tratados na Camada Bronze): {repetidos_id}")

    # O pandas trata valores ausentes como iguais entre si em duplicated/groupby,
    # o que é o comportamento desejado aqui (dois imóveis sem condomínio informado,
    # idênticos no resto, são o mesmo anúncio republicado).
    df["n_anuncios_iguais"] = df.groupby(CHAVE_DUPLICATA, dropna=False)["aluguel"].transform("size")
    duplicadas = df.duplicated(subset=CHAVE_DUPLICATA, keep="first")
    grupos = int((df["n_anuncios_iguais"] > 1).sum())
    rel.item(f"registros que coincidem em {' + '.join(CHAVE_DUPLICATA)}: {grupos}")
    rel.item(f"provável mesmo imóvel republicado ou anunciado por várias imobiliárias: {int(duplicadas.sum())} linhas excedentes")

    if manter:
        rel.item("--manter-duplicatas: nenhuma linha removida (apenas contabilizada em n_anuncios_iguais)")
        return df
    descartes.append(df[duplicadas].assign(motivo_descarte="duplicata de conteúdo (mesmo imóvel)"))
    resultado = df[~duplicadas].copy()
    rel.item(f"base após a deduplicação: {len(resultado)} imóveis únicos")
    return resultado


def marcar_outliers(df: pd.DataFrame, rel: Relatorio) -> pd.DataFrame:
    """Etapa 6 — sinaliza valores implausíveis, sem apagá-los."""
    rel.titulo("6. Marcação de outliers e inconsistências")

    motivos = [[] for _ in range(len(df))]
    posicao = {indice: k for k, indice in enumerate(df.index)}

    def marcar(condicao: pd.Series, texto: str) -> None:
        condicao = condicao.fillna(False)
        for indice in df.index[condicao]:
            motivos[posicao[indice]].append(texto)
        rel.item(f"{texto:<42}: {int(condicao.sum()):>5} registros")

    marcar(df["aluguel"] < LIMITES["aluguel_min"], f"aluguel abaixo de R$ {LIMITES['aluguel_min']}")
    marcar(df["aluguel"] > LIMITES["aluguel_max"], f"aluguel acima de R$ {LIMITES['aluguel_max']:,}".replace(",", "."))
    marcar(df["area_m2"] < LIMITES["area_min"], f"área abaixo de {LIMITES['area_min']} m²")
    marcar(df["area_m2"] > LIMITES["area_max"], f"área acima de {LIMITES['area_max']:,} m²".replace(",", "."))
    marcar(df["preco_m2"] < LIMITES["preco_m2_min"], f"preço por m² abaixo de R$ {LIMITES['preco_m2_min']}")
    marcar(df["preco_m2"] > LIMITES["preco_m2_max"], f"preço por m² acima de R$ {LIMITES['preco_m2_max']}")
    marcar(df["condominio"] > LIMITES["condominio_sobre_aluguel_max"] * df["aluguel"],
           f"condomínio acima de {LIMITES['condominio_sobre_aluguel_max']}x o aluguel")

    df["motivo_outlier"] = ["; ".join(m) for m in motivos]
    df["outlier"] = df["motivo_outlier"] != ""
    rel.item(f"total de registros marcados como outlier: {int(df['outlier'].sum())} ({df['outlier'].mean():.1%})")
    rel.item("nenhuma linha foi apagada por esta etapa — a exclusão é decidida na coluna apto_analise")
    return df


def definir_apto_analise(df: pd.DataFrame, rel: Relatorio, so_residencial: bool = False) -> pd.DataFrame:
    """Define o recorte analítico: sem outliers e com os campos essenciais preenchidos."""
    rel.titulo("7. Recorte analítico")
    apto = (~df["outlier"]) & df["aluguel"].notna() & df["area_m2"].notna() & df["preco_m2"].notna()
    rel.item(f"registros sem outlier e com aluguel e área: {int(apto.sum())}")
    if so_residencial:
        apto &= df["classe_imovel"] == "residencial"
        rel.item(f"--so-residencial: restrito a imóveis residenciais → {int(apto.sum())}")
    df["apto_analise"] = apto
    rel.item(f"apto_analise = verdadeiro em {int(apto.sum())} de {len(df)} registros ({apto.mean():.1%})")
    return df


# =============================================================================
# 4. PREPARAÇÃO DO CRUZAMENTO COM O IPS
# =============================================================================

def localizar(nomes: list[str]) -> Path | None:
    """Primeiro arquivo existente, entre os nomes aceitos, procurando em 0_entrada e
    depois na pasta do script."""
    for nome in nomes:
        for pasta in (PASTA_ENTRADA, PASTA):
            caminho = pasta / nome
            if caminho.exists():
                return caminho
    return None


def ler_tabela(caminho: Path) -> pd.DataFrame:
    """Lê CSV (detectando o separador) ou Excel."""
    if caminho.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(caminho, dtype=str)
    for sep in [";", ","]:
        try:
            df = pd.read_csv(caminho, dtype=str, sep=sep, encoding="utf-8-sig", keep_default_na=False)
            if df.shape[1] > 1:
                return df
        except Exception:  # noqa: BLE001
            continue
    return pd.read_csv(caminho, dtype=str, encoding="latin-1", keep_default_na=False)


def coluna_por_nome(df: pd.DataFrame, candidatos: list[str]) -> str | None:
    """Encontra a coluna cujo nome corresponde a um dos candidatos (sem acento/caixa)."""
    mapa = {normalizar_chave(c): c for c in df.columns}
    for candidato in candidatos:
        if normalizar_chave(candidato) in mapa:
            return mapa[normalizar_chave(candidato)]
    return None


def separar_bairros(texto: str) -> list[str]:
    """Divide a célula de bairros de uma região administrativa em nomes individuais.

    O de-para oficial lista os bairros de cada RA em uma única célula, em linguagem
    corrente: "Anil, Curicica, ... e Vila Valqueire". Há ainda frases descritivas
    ("Cidade Universitária e os catorze bairros da Ilha do Governador: Bancários, …")
    e casos sem lista ("Complexo de treze favelas que formam a Maré").
    """
    if not isinstance(texto, str) or not texto.strip():
        return []
    texto = limpar_texto(texto)
    # "e os catorze bairros da Ilha do Governador:" e afins viram simples separadores
    texto = re.sub(r"\s+e\s+os?\s+[\w\s]*?bairros?\s+d[aeo]s?[^:]*:", ", ", texto, flags=re.IGNORECASE)
    texto = texto.replace(":", ", ")
    # frase sem lista de bairros (ex.: "Complexo de treze favelas que formam a Maré")
    if re.search(r"favelas|complexo de", texto, re.IGNORECASE) and "," not in texto:
        return []
    partes = re.split(r",| e ", texto)
    return [limpar_texto(x) for x in partes if limpar_texto(x)]


def ler_depara(caminho: Path, rel: Relatorio) -> pd.DataFrame | None:
    """Lê o de-para bairro → região administrativa, nos dois formatos possíveis:
    uma linha por bairro, ou uma linha por RA com todos os seus bairros em uma célula.

    Devolve um quadro com bairro_chave e regiao_administrativa. Bairros homônimos em
    RAs diferentes (o caso de Freguesia, em Jacarepaguá e na Ilha do Governador) recebem
    apenas a chave qualificada ("freguesia jacarepagua"), que é a forma usada pelo portal.
    """
    tabela = ler_tabela(caminho)
    col_ra = coluna_por_nome(tabela, COLS_RA)
    col_lista = coluna_por_nome(tabela, COLS_LISTA_BAIRROS)
    if not col_ra or not col_lista:
        rel.item(f"{caminho.name}: não foi possível identificar as colunas de região administrativa e de "
                 f"bairro (encontradas: {list(tabela.columns)})")
        return None

    pares: list[tuple[str, str]] = []
    for _, linha in tabela.iterrows():
        ra = limpar_texto(str(linha[col_ra]))
        bairros = separar_bairros(str(linha[col_lista]))
        if not bairros:                     # RA sem lista de bairros: a própria RA é o bairro
            bairros = [ra]
        pares += [(b, ra) for b in bairros]

    contagem: dict[str, int] = {}
    for bairro, _ in pares:
        chave = normalizar_chave(bairro)
        contagem[chave] = contagem.get(chave, 0) + 1

    registros = []
    for bairro, ra in pares:
        chave = normalizar_chave(bairro)
        # nome qualificado pela RA, como o portal escreve os bairros homônimos
        registros.append({"bairro_chave": normalizar_chave(f"{bairro} {ra}"), "regiao_administrativa": ra})
        if contagem[chave] == 1:            # nome único: vale também a chave simples
            registros.append({"bairro_chave": chave, "regiao_administrativa": ra})

    homonimos = sorted({b for b, _ in pares if contagem[normalizar_chave(b)] > 1})
    rel.item(f"{caminho.name}: {len(tabela)} regiões administrativas, {len(pares)} bairros")
    if homonimos:
        rel.item(f"bairros homônimos em mais de uma RA (resolvidos pelo nome qualificado): {', '.join(homonimos)}")
    return pd.DataFrame(registros).drop_duplicates("bairro_chave")


def ler_ips(caminho: Path, rel: Relatorio) -> pd.DataFrame | None:
    """Lê a nota do IPS por região administrativa.

    Reconhece a planilha oficial do Data.Rio — em que os dados ficam na aba
    "Dimensões e Componentes", sob um cabeçalho de várias linhas, e o nome da RA vem
    precedido do numeral romano ("IV BOTAFOGO") — e também um arquivo simples com uma
    coluna de região e outra de nota.
    """
    if caminho.suffix.lower() in (".xlsx", ".xls"):
        with pd.ExcelFile(caminho) as planilha:      # fecha o arquivo logo após ler os nomes
            abas = planilha.sheet_names
        aba = next((a for a in abas if ABA_IPS in normalizar_chave(a)), None)
        if aba:
            return ler_ips_datario(caminho, aba, rel)

    tabela = ler_tabela(caminho)
    col_ra = coluna_por_nome(tabela, COLS_RA + COLS_BAIRRO)
    col_ips = coluna_por_nome(tabela, COLS_IPS)
    if not col_ra or not col_ips:
        rel.item(f"{caminho.name}: não foi possível identificar as colunas de região administrativa e de nota "
                 f"do IPS (encontradas: {list(tabela.columns)[:12]})")
        return None
    saida = pd.DataFrame({
        "ra_chave": tabela[col_ra].map(nome_sem_numeral).map(normalizar_chave),
        "ips": pd.to_numeric(tabela[col_ips].astype(str).str.replace(",", ".", regex=False), errors="coerce"),
    })
    rel.item(f"{caminho.name}: {len(saida)} regiões com nota do IPS (coluna \"{col_ips}\")")
    return saida.dropna(subset=["ips"]).drop_duplicates("ra_chave")


def ler_ips_datario(caminho: Path, aba: str, rel: Relatorio) -> pd.DataFrame | None:
    """Extrai o IPS geral e as três dimensões da planilha do Data.Rio."""
    bruto = pd.read_excel(caminho, sheet_name=aba, header=None, dtype=str)

    # a linha de cabeçalho é a que contém "Regiões Administrativas"
    linha_cab = next((i for i in range(len(bruto))
                      if normalizar_chave(str(bruto.iloc[i, 0])) == ROTULO_RA_IPS), None)
    if linha_cab is None:
        rel.item(f"{caminho.name}: aba \"{aba}\" lida, mas o cabeçalho \"Regiões Administrativas\" não foi encontrado")
        return None

    colunas: dict[str, int] = {}
    for indice, valor in enumerate(bruto.iloc[linha_cab].tolist()):
        chave = normalizar_chave(str(valor))
        for rotulo, nome in ROTULOS_DIMENSAO.items():
            if chave.startswith(rotulo) and nome not in colunas:
                colunas[nome] = indice
    if "ips" not in colunas:
        rel.item(f"{caminho.name}: coluna do IPS geral não localizada na aba \"{aba}\"")
        return None

    dados = bruto.iloc[linha_cab + 1:].copy()
    saida = pd.DataFrame({"ra_bruta": dados.iloc[:, 0]})
    for nome, indice in colunas.items():
        saida[nome] = pd.to_numeric(dados.iloc[:, indice].astype(str).str.replace(",", ".", regex=False), errors="coerce")
    saida = saida.dropna(subset=["ips"])
    saida["ra_chave"] = saida["ra_bruta"].map(nome_sem_numeral).map(normalizar_chave)
    # a primeira linha costuma ser o total do município, que não é uma RA
    cidade = saida["ra_chave"] == normalizar_chave(MUNICIPIO_ALVO)
    if cidade.any():
        rel.item(f"referência da cidade: IPS {float(saida.loc[cidade, 'ips'].iloc[0]):.1f} (linha excluída da junção)")
        saida = saida[~cidade]
    rel.item(f"{caminho.name}: {len(saida)} regiões administrativas na aba \"{aba}\" "
             f"({', '.join(c for c in colunas if c != 'ips')} também importadas)")
    return saida.drop(columns=["ra_bruta"]).drop_duplicates("ra_chave")


def nome_sem_numeral(texto: str) -> str:
    """Remove o numeral romano que precede o nome da RA: "XXXIV CIDADE DE DEUS" → "CIDADE DE DEUS"."""
    if not isinstance(texto, str):
        return ""
    return re.sub(r"^\s*[IVXLC]+\s*[-–.]?\s+", "", limpar_texto(texto))


def exportar_lista_bairros(df: pd.DataFrame, rel: Relatorio) -> None:
    """Gera a lista de bairros da base, para preencher o de-para com as regiões administrativas."""
    lista = (df.groupby(["bairro", "bairro_chave"])
               .agg(anuncios=("aluguel", "size"), aluguel_mediano=("aluguel", "median"))
               .reset_index().sort_values("bairro"))
    lista["regiao_administrativa"] = ""   # coluna a ser preenchida
    lista.to_csv(PASTA_SAIDA / ARQUIVO_BAIRROS, index=False, encoding=CODIFICACAO)
    rel.item(f"{ARQUIVO_BAIRROS}: {len(lista)} bairros listados para o preenchimento do de-para")


def juntar_ips(df: pd.DataFrame, rel: Relatorio) -> pd.DataFrame:
    """Etapa 8 — de-para bairro → região administrativa e junção com o IPS, se os arquivos existirem."""
    rel.titulo("8. Cruzamento com o IPS")
    exportar_lista_bairros(df, rel)

    # --- de-para bairro → região administrativa ---
    caminho_depara = localizar(NOMES_DEPARA)
    if caminho_depara is None:
        rel.item("de-para bairro → região administrativa não encontrado na pasta; etapa adiada")
        rel.item(f"quando tiver o arquivo, salve-o como um destes nomes: {', '.join(NOMES_DEPARA[:2])}")
        return df

    depara = ler_depara(caminho_depara, rel)
    if depara is None:
        return df

    df = df.merge(depara, on="bairro_chave", how="left")
    sem_ra = df["regiao_administrativa"].isna() | (df["regiao_administrativa"] == "")
    rel.item(f"de-para aplicado: {int((~sem_ra).sum())} anúncios ({(~sem_ra).mean():.1%}) com região administrativa")
    if sem_ra.any():
        faltantes = (df.loc[sem_ra].groupby("bairro").size().sort_values(ascending=False))
        rel.item(f"bairros sem correspondência ({len(faltantes)}), com o número de anúncios:")
        for bairro, quantos in faltantes.items():
            rel.item(f"    {bairro} ({quantos})")

    # --- nota do IPS por região administrativa ---
    caminho_ips = localizar(NOMES_IPS)
    if caminho_ips is None:
        rel.item("base do IPS não encontrada na pasta; junção adiada")
        return df

    ips = ler_ips(caminho_ips, rel)
    if ips is None:
        return df

    colunas_ips = [c for c in ips.columns if c != "ra_chave"]
    df["ra_chave"] = df["regiao_administrativa"].fillna("").map(normalizar_chave)
    df = df.merge(ips, on="ra_chave", how="left")
    encontrados = int(df["ips"].notna().sum())

    # A base do IPS pode vir aberta por bairro, e não por região administrativa:
    # se a junção pela RA não encontrar nada, tenta pelo próprio bairro.
    if encontrados == 0:
        df = df.drop(columns=colunas_ips)
        df = df.merge(ips.rename(columns={"ra_chave": "bairro_chave"}), on="bairro_chave", how="left")
        encontrados = int(df["ips"].notna().sum())
        rel.item("junção pela região administrativa não encontrou correspondências; usada a junção por bairro")

    rel.item(f"IPS aplicado: {encontrados} anúncios ({encontrados / len(df):.1%}) com nota do IPS")
    if encontrados:
        rel.item(f"nota do IPS entre {df['ips'].min():.1f} e {df['ips'].max():.1f}; "
                 f"colunas acrescentadas: {', '.join(colunas_ips)}")
        sem_nota = sorted(df.loc[df["ips"].isna() & (~sem_ra), "regiao_administrativa"].dropna().unique())
        if sem_nota:
            rel.item(f"regiões administrativas sem nota na base do IPS: {', '.join(sem_nota)}")
    else:
        rel.item("ATENÇÃO: nenhuma correspondência — confira se os nomes das regiões coincidem entre os arquivos")
    return df


# =============================================================================
# 5. ORQUESTRAÇÃO
# =============================================================================

def interpretar_argumentos(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Camada Prata — tratamento da base bruta do ZAP Imóveis.")
    parser.add_argument("--entrada", default=ARQUIVO_ENTRADA, help=f"CSV bruto de entrada (padrão: {ARQUIVO_ENTRADA})")
    parser.add_argument("--saida", default=ARQUIVO_SAIDA, help=f"CSV tratado de saída (padrão: {ARQUIVO_SAIDA})")
    parser.add_argument("--so-residencial", action="store_true",
                        help="restringe o recorte analítico aos imóveis residenciais")
    parser.add_argument("--manter-duplicatas", action="store_true",
                        help="não remove duplicatas de conteúdo (apenas as contabiliza)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = interpretar_argumentos(argv)
    for fluxo in (sys.stdout, sys.stderr):          # console do Windows em UTF-8
        try:
            fluxo.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    entrada = Path(args.entrada)
    if not entrada.is_absolute() and not entrada.exists():
        for pasta in (PASTA_BRONZE, PASTA):
            if (pasta / args.entrada).exists():
                entrada = pasta / args.entrada
                break
        else:
            entrada = PASTA_BRONZE / args.entrada
    if not entrada.exists():
        print(f"ERRO: arquivo de entrada não encontrado: {entrada}")
        return 1

    rel = Relatorio()
    rel.titulo("0. Leitura da base bruta")
    df = pd.read_csv(entrada, dtype=str, encoding=CODIFICACAO, keep_default_na=False)
    df = df.rename(columns={"etiqueta_preco": "etiqueta_preco_orig"})
    bruto = len(df)
    rel.item(f"arquivo: {entrada.name}")
    rel.item(f"{bruto} registros e {df.shape[1]} colunas")

    descartes: list[pd.DataFrame] = []
    df = normalizar_textos(df, rel)
    df = filtrar_municipio(df, descartes, rel)
    df = converter_numeros(df, rel)
    df = derivar_atributos(df, rel)
    df = remover_duplicatas(df, descartes, rel, manter=args.manter_duplicatas)
    df = marcar_outliers(df, rel)
    df = definir_apto_analise(df, rel, so_residencial=args.so_residencial)
    df = juntar_ips(df, rel)

    # --- Saídas ---
    rel.titulo("9. Arquivos gerados")
    colunas = [c for c in COLUNAS_SAIDA if c in df.columns]
    colunas += [c for c in ("regiao_administrativa", "ips", "ips_necessidades_basicas",
                            "ips_bem_estar", "ips_oportunidades") if c in df.columns]
    saida = PASTA_SAIDA / args.saida
    df[colunas].to_csv(saida, index=False, encoding=CODIFICACAO)
    rel.item(f"{saida.name}: {len(df)} registros tratados, {len(colunas)} colunas")

    analise = df[df["apto_analise"]]
    analise[colunas].to_csv(PASTA_SAIDA / ARQUIVO_ANALISE, index=False, encoding=CODIFICACAO)
    rel.item(f"{ARQUIVO_ANALISE}: {len(analise)} registros aptos à análise")

    if descartes:
        todos = pd.concat(descartes, ignore_index=True)
        cols_desc = [c for c in ("motivo_descarte", "bairro", "rua", "aluguel", "condominio", "area_m2",
                                 "quartos", "id_anuncio", "url_anuncio") if c in todos.columns]
        todos[cols_desc].to_csv(PASTA_SAIDA / ARQUIVO_DESCARTES, index=False, encoding=CODIFICACAO)
        rel.item(f"{ARQUIVO_DESCARTES}: {len(todos)} registros descartados, com o motivo")

    rel.titulo("Resumo")
    n_fora = sum(len(d) for d in descartes if "município" in str(d["motivo_descarte"].iloc[0]))
    n_dup = sum(len(d) for d in descartes if "duplicata" in str(d["motivo_descarte"].iloc[0]))
    rel.item(f"base bruta ................... {bruto}")
    rel.item(f"(-) fora do município ........ {n_fora}")
    rel.item(f"(-) duplicatas de conteúdo ... {n_dup}")
    rel.item(f"base tratada ................. {len(df)}")
    rel.item(f"    dos quais outliers ....... {int(df['outlier'].sum())} (marcados, não apagados)")
    rel.item(f"aptos à análise .............. {len(analise)} ({len(analise) / bruto:.1%} da base bruta)")
    rel.salvar(PASTA_SAIDA / ARQUIVO_RELATORIO)
    print(f"\nRelatório completo em {ARQUIVO_RELATORIO}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
