# -*- coding: utf-8 -*-
"""
zap_scraper.py — Camada Bronze: extração de anúncios de locação no ZAP Imóveis (Rio de Janeiro)
================================================================================================

Robô de coleta para o TCC. Percorre uma lista de bairros, aplica a ordenação
"Mais recente" (controle de viés de anúncios patrocinados), raspa até N páginas
por bairro e grava os dados BRUTOS em CSV. Nenhum tratamento numérico é feito
aqui — isso é papel da Camada Prata.

Stack: Python 3.10+ · undetected-chromedriver · BeautifulSoup · Pandas

Uso:
    python zap_scraper.py                      # execução completa (30 páginas/bairro)
    python zap_scraper.py --teste              # 1 página por bairro, para validar o ambiente
    python zap_scraper.py --bairros Urca       # só os bairros informados
    python zap_scraper.py --max-paginas 5      # limite de páginas diferente do padrão
    python zap_scraper.py --todos              # COLETA COMPLETA: todos os bairros de bairros_rj.csv
    python zap_scraper.py --todos --zonas zona-sul zona-central   # só algumas zonas
    python zap_scraper.py --todos --continuar  # retoma de onde parou (CSV + progresso_coleta.json)
    python zap_scraper.py --consolidar         # junta cópias de emergência no CSV principal
    python zap_scraper.py --todos --refazer Copacabana Pavuna   # recoleta só esses bairros (mantendo o CSV)

Organização do arquivo:
    1. CONFIGURAÇÃO ............ tudo que pode precisar de ajuste (bairros, seletores, esperas)
    2. UTILITÁRIOS ............. limpeza de texto, esperas aleatórias, logging
    3. CAMADA DE NAVEGAÇÃO ..... undetected-chromedriver, cookies, anti-bot, carregamento
    4. CAMADA DE PARSING ....... BeautifulSoup → dicionários (uma função por atributo)
    5. ORQUESTRAÇÃO ............ laço bairro → página → cards, deduplicação, checkpoint em CSV
"""

from __future__ import annotations

import argparse
import copy
import csv
import importlib.util
import json
import logging
import random
import re
import shutil
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

import pandas as pd
import undetected_chromedriver as uc
from bs4 import BeautifulSoup, Tag
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


def _encerramento_silencioso(self) -> None:
    """Substitui o destrutor do uc.Chrome, que no Windows tenta fechar o Chrome uma
    segunda vez (já fechado por encerrar_navegador) e imprime um
    "OSError: [WinError 6]" inofensivo ao final da execução."""
    try:
        self.quit()
    except Exception:  # noqa: BLE001
        pass


uc.Chrome.__del__ = _encerramento_silencioso

# =============================================================================
# 1. CONFIGURAÇÃO
# =============================================================================

BASE_URL = "https://www.zapimoveis.com.br"
TRANSACAO = "aluguel"

# --- Pastas do projeto -------------------------------------------------------
# O projeto é organizado em camadas: 0_entrada guarda os insumos externos e cada
# camada numerada guarda o seu script e as suas saídas. Se a estrutura em pastas não
# existir (todos os arquivos soltos em uma pasta só), tudo cai na pasta do script.
PASTA = Path(__file__).resolve().parent

def pasta_do_projeto(nome: str) -> Path:
    candidata = PASTA.parent / nome
    return candidata if candidata.is_dir() else PASTA

PASTA_ENTRADA = pasta_do_projeto("0_entrada")     # insumos que não são gerados por código
PASTA_SAIDA = PASTA                               # esta camada escreve na própria pasta

# Bairros da amostra de teste. O "slug" é o trecho que o próprio ZAP usa na URL, no
# padrão rj+rio-de-janeiro+<zona>+<bairro>. Para incluir outro bairro, faça a busca
# no site e copie o trecho da URL que vem depois de "/aluguel/imoveis/".
BAIRROS = [
    {"nome": "Copacabana", "zona": "zona-sul",   "slug": "rj+rio-de-janeiro+zona-sul+copacabana"},
    {"nome": "Tijuca",     "zona": "zona-norte", "slug": "rj+rio-de-janeiro+zona-norte+tijuca"},
    {"nome": "Urca",       "zona": "zona-sul",   "slug": "rj+rio-de-janeiro+zona-sul+urca"},
]
# Coleta completa (--todos): todos os bairros do município com página de busca de
# aluguel no portal (162 em 06/09/2026), levantados do "Mapa do site" e do "Guia de
# bairros" do ZAP. Colunas: nome, zona, slug, fonte, observacao.
NOME_BAIRROS = "bairros_rj.csv"
ARQUIVO_BAIRROS = str(PASTA_ENTRADA / NOME_BAIRROS)
# Progresso da coleta completa (bairros concluídos) — permite retomar com --continuar
ARQUIVO_PROGRESSO = str(PASTA_SAIDA / "progresso_coleta.json")

# REGRA DE OURO ESTATÍSTICA — ordenação por "Mais recente".
# Verificado no DOM real em 06/09/2026: o site controla a ordenação pelo parâmetro
# de URL `ordem`. O padrão do site é MOST_RELEVANT, que empurra anúncios
# "Destaque"/"Super Destaque" (pagos) para o topo; MOST_RECENT elimina esse viés.
PARAMETROS_URL_FIXOS = {"ordem": "MOST_RECENT"}
PARAMETRO_PAGINA = "pagina"            # paginação também é por URL: &pagina=2, &pagina=3 ...

# Critério de parada por bairro
MAX_PAGINAS_POR_BAIRRO = 30            # ~30 cards/página → ~700–900 anúncios por bairro

# Evasão anti-scraping — tempos em segundos (mínimo, máximo)
ESPERA_ENTRE_PAGINAS = (4, 9)          # sleep aleatório entre requisições (regra do projeto)
ESPERA_ENTRE_BAIRROS = (10, 20)        # pausa maior ao trocar de bairro
ESPERA_DESAFIO_ANTIBOT = (15, 30)      # quando a Cloudflare exibe a tela de verificação
TIMEOUT_CARREGAMENTO = 30              # quanto esperar pelos cards antes de tentar de novo
TENTATIVAS_POR_PAGINA = 3
ESPERA_APOS_FALHA = (20, 40)           # pausa maior antes de repetir uma página que falhou
MAX_PASSOS_ROLAGEM = 8                 # rolagem "humana" da página após o carregamento
MAX_BAIRROS_ABORTADOS_SEGUIDOS = 3     # bairros seguidos sem conseguir carregar → provável bloqueio, encerra

# Coletas longas: o Chrome vai ficando lento (memória) e pode parar de responder.
# O navegador é reiniciado de tempos em tempos e sempre que travar.
REINICIAR_NAVEGADOR_A_CADA = 20        # bairros

# Navegador
CHROME_VERSION_MAIN: int | None = None  # ex.: 140 — preencha só se o uc reclamar da versão do Chrome
IDIOMA_NAVEGADOR = "pt-BR"

# Saída
ARQUIVO_SAIDA = str(PASTA_SAIDA / "zap_imoveis_raw_rj.csv")
ARQUIVO_LOG = str(PASTA_SAIDA / "zap_scraper.log")
SEPARADOR_CSV = ","
CODIFICACAO_CSV = "utf-8-sig"          # com BOM: o Excel abre os acentos corretamente

# Colunas exigidas pelo projeto (sempre presentes, nesta ordem)
COLUNAS_OBRIGATORIAS = ["bairro", "valor_aluguel", "valor_condominio", "area_util", "quartos"]
# Colunas extras de rastreabilidade (linhagem de dados da Camada Bronze). Úteis para
# a Camada Prata deduplicar, filtrar anúncios pagos e auditar a coleta.
COLUNAS_RASTREABILIDADE = [
    "bairro_busca",   # bairro pesquisado (o site inclui bairros vizinhos nos resultados)
    "rua",
    "valor_iptu",
    "tipo_anuncio",   # data-type do card: "SUPER PREMIUM", "FIXED TOP"... (vazio = orgânico)
    "tag_destaque",   # "Destaque" / "Super Destaque"
    "anuncios_agrupados",  # "2" quando o ZAP funde anúncios do mesmo imóvel em um card (sem id/url)
    "etiqueta_preco", # "Ótimo preço" / "Baixou de preço" (etiqueta exibida junto ao preço)
    "id_anuncio",
    "url_anuncio",
    "pagina",
    "data_coleta",
]
INCLUIR_COLUNAS_RASTREABILIDADE = True  # False → o CSV sai exatamente com as 5 colunas obrigatórias
DEDUPLICAR_POR_ID = True                # mesmo anúncio repetido (cards agrupados/destaques) entra 1 vez

# -----------------------------------------------------------------------------
# 1.1 Seletores CSS (sintaxe do BeautifulSoup/Selenium)
#
# O ZAP ofusca as classes CSS, mas marca os elementos com atributos `data-cy`
# (usados pelos testes automatizados deles), que são muito mais estáveis.
# Se o site mudar, inspecione um card no navegador (F12) e ajuste SOMENTE aqui.
# Mapeamento verificado no DOM real em 06/09/2026.
# -----------------------------------------------------------------------------
SELETORES = {
    # Container de cada anúncio na lista de resultados
    "card":             'li[data-cy="rp-property-cd"]',
    # Um card normalmente tem 1 anúncio, mas cards "agrupados" (mesma imobiliária)
    # trazem vários; cada anúncio é um link para /imovel/... com seu próprio preço.
    "link_anuncio":     'a[href*="/imovel/"]',
    # ---- Atributos alvo ----
    "localizacao":      '[data-cy="rp-cardProperty-location-txt"]',      # "Copacabana, Rio de Janeiro"
    "rua":              '[data-cy="rp-cardProperty-street-txt"]',
    "bloco_preco":      '[data-cy="rp-cardProperty-price-txt"]',         # "R$ 3.800 /mês  Cond. R$ 800 • IPTU R$ 104"
    "area_util":        '[data-cy="rp-cardProperty-propertyArea-txt"]',  # "36 m²"
    "quartos":          '[data-cy="rp-cardProperty-bedroomQuantity-txt"]',  # ausente em kitnets/salas comerciais
    # ---- Metadados para o controle de viés ----
    "tag_destaque":     '[data-cy="rp-cardProperty-tag-txt"]',
    # Card "deduplicado": o ZAP funde anúncios do mesmo imóvel publicados por
    # anunciantes diferentes. Ele não tem link (abre um modal), a área vira uma
    # faixa ("130 - 135 m²") e o botão diz "Ver os 2 anúncios deste imóvel".
    "botao_agrupado":   '[data-cy="listing-card-deduplicated-button"]',
    # ---- Estrutura da página ----
    "blocos_ignorados": '[data-cy="recommendations-list"]',   # recomendações de outros bairros: NÃO são resultados
    "titulo_busca":     '[data-cy="rp-searchTitle-txt"]',     # "1.122 Imóveis para alugar em ..."
}

# Os valores vêm acompanhados de rótulos acessíveis e ícones, que precisam ser removidos:
#   <h3><span class="sr-only">Quantidade de quartos </span><svg .../>2</h3>  →  "2"
#   <h2><span>Apartamento para alugar com 36 m² ... em </span>Copacabana, Rio de Janeiro</h2>
TAGS_ROTULO = ["span", "svg"]

# Expressões regulares aplicadas ao TEXTO do bloco de preço
REGEX_VALOR_MONETARIO = re.compile(r"R\$\s?[\d.]+(?:,\d{1,2})?")
# Condomínio/IPTU vêm como valor ("R$ 800") ou status ("isento", "não informado").
# O bloco de preço pode terminar com uma etiqueta ("Ótimo preço", "Baixou de preço"),
# que NÃO pode ser confundida com o valor: por isso o valor é lido de forma estrita
# e o que sobra depois dele vira a coluna etiqueta_preco.
REGEX_VALOR_OU_STATUS = re.compile(r"R\$\s?[\d.]+(?:,\d{1,2})?|isento|n[ãa]o informado|sob consulta|a combinar", re.IGNORECASE)
REGEX_ROTULO_CONDOMINIO = re.compile(r"Cond(?:om[ií]nio)?\.?\s*:?\s*", re.IGNORECASE)
REGEX_ROTULO_IPTU = re.compile(r"IPTU\s*:?\s*", re.IGNORECASE)
REGEX_ATE_SEPARADOR = re.compile(r"([^•|\n]+?)(?=\s*(?:•|\||IPTU|$))", re.IGNORECASE)
REGEX_ID_ANUNCIO = re.compile(r"id-(\d+)")     # .../copacabana-...-36m2-id-2886901016/
REGEX_ANUNCIOS_AGRUPADOS = re.compile(r"(\d+)\s+an[úu]ncios", re.IGNORECASE)   # "Ver os 2 anúncios deste imóvel"

# Pop-up de cookies — tentado nesta ordem. O primeiro é o ID real do banner do ZAP
# (plataforma AdOpt); os demais são fallbacks genéricos. O XPath exige que o texto
# COMECE com "aceitar", para nunca clicar em um "Não aceitar".
SELETORES_COOKIES = [
    (By.ID, "adopt-accept-all-button"),
    (By.CSS_SELECTOR, "#onetrust-accept-btn-handler, button[id*='accept' i], button[class*='accept' i]"),
    (By.XPATH, "//button[starts-with(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
               "'abcdefghijklmnopqrstuvwxyz'), 'aceitar')]"),
]

# Sinais (em minúsculas) de que a Cloudflare exibiu uma tela de verificação
SINAIS_DESAFIO_ANTIBOT_TITULO = ("just a moment", "um momento", "attention required", "access denied")
SINAIS_DESAFIO_ANTIBOT_CORPO = (
    "verify you are human", "verifique se você é humano", "verifique que você é humano",
    "checking your browser", "verificando seu navegador", "enable javascript and cookies to continue",
)
# Sinais de que a busca não tem (mais) resultados
SINAIS_SEM_RESULTADOS = ("não encontramos imóveis", "nenhum imóvel encontrado", "nenhum resultado")

# Parser HTML: lxml é mais rápido; cai para o parser nativo se não estiver instalado
PARSER_HTML = "lxml" if importlib.util.find_spec("lxml") else "html.parser"


# =============================================================================
# 2. UTILITÁRIOS
# =============================================================================

def configurar_logging(arquivo_log: str = ARQUIVO_LOG) -> None:
    """Log no console e em arquivo (o arquivo serve de evidência da coleta no TCC)."""
    # Console do Windows às vezes usa cp1252; força UTF-8 para não quebrar acentos.
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(arquivo_log, encoding="utf-8"),
        ],
        force=True,   # substitui handlers pré-existentes (ex.: ao rodar em notebook)
    )
    # Silencia o ruído das bibliotecas de terceiros
    for nome in ("selenium", "urllib3", "undetected_chromedriver"):
        logging.getLogger(nome).setLevel(logging.WARNING)


def limpar_texto(texto: str | None) -> str | None:
    """Garante que o texto não venha "quebrado", sem alterar o conteúdo.

    Normaliza Unicode (NFC), troca espaços não separáveis (\\xa0 — muito comum em
    "R$ 3.800") por espaço comum e colapsa espaços/quebras de linha repetidos.
    Retorna None para texto vazio, o que vira célula vazia no CSV.
    """
    if texto is None:
        return None
    texto = unicodedata.normalize("NFC", str(texto))
    texto = texto.replace("\xa0", " ").replace("\u200b", "")   # nbsp e zero-width space
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto or None


def espera_aleatoria(intervalo: tuple[float, float], motivo: str = "") -> None:
    """Pausa aleatória dentro do intervalo (segundos), imitando o ritmo de um usuário."""
    segundos = random.uniform(*intervalo)
    logging.info("Aguardando %.1fs %s", segundos, motivo)
    time.sleep(segundos)


# =============================================================================
# 3. CAMADA DE NAVEGAÇÃO (undetected-chromedriver)
# =============================================================================

def iniciar_navegador() -> uc.Chrome:
    """Sobe o Chrome com o undetected-chromedriver, maximizado e com as opções padrão de evasão.

    Observações:
    - NÃO usa modo headless de propósito: headless é o sinal mais forte de automação
      para a Cloudflare. A janela fica visível durante a coleta.
    - O próprio uc já remove os sinais clássicos (navigator.webdriver, flags do
      chromedriver etc.); não adicione `excludeSwitches`/`useAutomationExtension`,
      pois conflitam com os patches dele.
    """
    opcoes = uc.ChromeOptions()
    opcoes.add_argument("--start-maximized")
    opcoes.add_argument(f"--lang={IDIOMA_NAVEGADOR}")
    opcoes.add_argument("--no-first-run")
    opcoes.add_argument("--no-default-browser-check")
    opcoes.add_argument("--disable-popup-blocking")
    # 'eager': driver.get() retorna no DOMContentLoaded, sem esperar anúncios, mapas e
    # imagens terminarem. Os cards vêm no HTML do servidor, e aguardar_resultados()
    # confirma que eles estão na página. Deixa cada página 2-3x mais leve.
    opcoes.page_load_strategy = "eager"

    driver = uc.Chrome(
        options=opcoes,
        version_main=CHROME_VERSION_MAIN,   # None → detecta a versão do Chrome instalada
        headless=False,
        use_subprocess=True,
    )
    driver.set_page_load_timeout(60)
    try:
        driver.maximize_window()            # garante a maximização mesmo se o argumento for ignorado
    except WebDriverException:
        pass
    logging.info("Navegador iniciado (Chrome via undetected-chromedriver).")
    return driver


def encerrar_navegador(driver: uc.Chrome | None) -> None:
    """Fecha o navegador ignorando erros de encerramento (comuns no Windows com o uc)."""
    if driver is None:
        return
    try:
        driver.quit()
    except Exception:  # noqa: BLE001 — encerramento nunca deve derrubar o script
        pass


class NavegadorTravado(RuntimeError):
    """O Chrome/chromedriver parou de responder; é preciso reiniciá-lo."""
    pagina: int | None = None


def navegador_responde(driver: uc.Chrome) -> bool:
    """Teste rápido de saúde do navegador."""
    try:
        return driver.execute_script("return 1") == 1
    except Exception:  # noqa: BLE001
        return False


def reiniciar_navegador(driver: uc.Chrome | None, estado: dict, motivo: str) -> uc.Chrome:
    """Fecha e reabre o Chrome (libera memória / recupera de travamento)."""
    logging.info("Reiniciando o navegador (%s).", motivo)
    encerrar_navegador(driver)
    time.sleep(random.uniform(3, 6))
    novo = iniciar_navegador()
    estado["cookies_verificados"] = False      # perfil novo → o banner de cookies volta
    return novo


def pagina_com_desafio_antibot(driver: uc.Chrome) -> bool:
    """Detecta a tela de verificação da Cloudflare ("Just a moment...", "Verify you are human")."""
    try:
        titulo = (driver.title or "").lower()
        if any(sinal in titulo for sinal in SINAIS_DESAFIO_ANTIBOT_TITULO):
            return True
        # Só examina o corpo se ainda não há cards (páginas de desafio são curtas)
        if driver.find_elements(By.CSS_SELECTOR, SELETORES["card"]):
            return False
        corpo = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
        return any(sinal in corpo for sinal in SINAIS_DESAFIO_ANTIBOT_CORPO)
    except WebDriverException:
        return False


def aceitar_cookies(driver: uc.Chrome, timeout: float = 6) -> bool:
    """Fecha o pop-up de cookies clicando em "Aceitar", se ele existir.

    timeout > 0  → espera o botão aparecer (use na primeira página da execução).
    timeout == 0 → verificação instantânea, sem espera (páginas seguintes).
    """
    for por, seletor in SELETORES_COOKIES:
        try:
            if timeout > 0:
                botao = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((por, seletor)))
            else:
                encontrados = [b for b in driver.find_elements(por, seletor) if b.is_displayed()]
                if not encontrados:
                    continue
                botao = encontrados[0]
            try:
                botao.click()
            except WebDriverException:
                driver.execute_script("arguments[0].click();", botao)  # clique via JS se algo cobrir o botão
            logging.info("Pop-up de cookies aceito (%s).", seletor)
            time.sleep(random.uniform(0.8, 1.6))
            return True
        except TimeoutException:
            continue
        except WebDriverException as erro:
            logging.debug("Não foi possível clicar no botão de cookies %s: %s", seletor, erro)
    return False


def aguardar_resultados(driver: uc.Chrome) -> str:
    """Espera a lista de resultados renderizar. Retorna "cards", "vazio" ou "timeout"."""
    def condicao(d):
        if d.find_elements(By.CSS_SELECTOR, SELETORES["card"]):
            return "cards"
        corpo = (d.find_element(By.TAG_NAME, "body").text or "").lower()
        if any(sinal in corpo for sinal in SINAIS_SEM_RESULTADOS):
            return "vazio"
        return False

    try:
        return WebDriverWait(driver, TIMEOUT_CARREGAMENTO, poll_frequency=1).until(condicao)
    except TimeoutException:
        return "timeout"


def rolar_pagina_como_humano(driver: uc.Chrome) -> None:
    """Rola a página em passos irregulares: dispara o lazy-loading e imita um usuário real."""
    try:
        altura_total = driver.execute_script("return document.body.scrollHeight") or 0
        posicao = 0
        for _ in range(MAX_PASSOS_ROLAGEM):
            if posicao >= altura_total:
                break
            posicao += random.randint(600, 1200)
            driver.execute_script("window.scrollTo(0, arguments[0]);", posicao)
            time.sleep(random.uniform(0.2, 0.6))
            altura_total = driver.execute_script("return document.body.scrollHeight") or altura_total
    except WebDriverException as erro:
        logging.debug("Falha ao rolar a página (ignorada): %s", erro)


def carregar_pagina(driver: uc.Chrome, url: str, esperar_cookies: bool) -> str | None:
    """Abre a URL, trata cookies/anti-bot e devolve o HTML renderizado (ou None após esgotar as tentativas)."""
    for tentativa in range(1, TENTATIVAS_POR_PAGINA + 1):
        try:
            driver.get(url)
        except TimeoutException:
            logging.warning("Timeout ao abrir a página (tentativa %d/%d).", tentativa, TENTATIVAS_POR_PAGINA)
            if not navegador_responde(driver):
                raise NavegadorTravado("timeout e o navegador não responde")
            continue
        except Exception as erro:  # noqa: BLE001 — inclui erros de socket entre o Python e o chromedriver
            logging.warning("Erro do navegador ao abrir a página (tentativa %d/%d): %s",
                            tentativa, TENTATIVAS_POR_PAGINA, str(erro).splitlines()[0][:200])
            if not navegador_responde(driver):
                raise NavegadorTravado(type(erro).__name__)
            espera_aleatoria(ESPERA_ENTRE_PAGINAS, "(após erro)")
            continue

        if pagina_com_desafio_antibot(driver):
            # A verificação automática da Cloudflare costuma concluir sozinha em alguns
            # segundos. Se aparecer um desafio interativo, resolva-o manualmente na janela
            # do navegador — o script continua esperando e tenta de novo.
            logging.warning("Tela de verificação anti-bot detectada (tentativa %d/%d). "
                            "Aguardando; se houver um desafio na janela do Chrome, resolva-o manualmente.",
                            tentativa, TENTATIVAS_POR_PAGINA)
            espera_aleatoria(ESPERA_DESAFIO_ANTIBOT, "(verificação anti-bot)")
            if pagina_com_desafio_antibot(driver):
                espera_aleatoria(ESPERA_DESAFIO_ANTIBOT, "(verificação anti-bot, 2ª espera)")
                continue

        estado = aguardar_resultados(driver)
        if estado == "timeout":
            logging.warning("Os cards não apareceram em %ds (tentativa %d/%d). Verifique a janela do Chrome.",
                            TIMEOUT_CARREGAMENTO, tentativa, TENTATIVAS_POR_PAGINA)
            espera_aleatoria(ESPERA_ENTRE_PAGINAS, "(nova tentativa)")
            continue

        # Com a página renderizada, o banner de cookies (se houver) já está na tela:
        # na 1ª página da execução espera um pouco por ele; nas demais só confere.
        aceitar_cookies(driver, timeout=3 if esperar_cookies else 0)

        rolar_pagina_como_humano(driver)
        try:
            return driver.page_source
        except Exception as erro:  # noqa: BLE001
            logging.warning("Não foi possível ler o HTML da página (tentativa %d/%d): %s",
                            tentativa, TENTATIVAS_POR_PAGINA, str(erro).splitlines()[0][:200])
            if not navegador_responde(driver):
                raise NavegadorTravado(type(erro).__name__)

    return None


# =============================================================================
# 4. CAMADA DE PARSING (BeautifulSoup)
#    Cada atributo tem sua própria extração isolada em try/except: se um card não
#    tiver condomínio (ou quartos, ou área), os demais campos são preservados.
# =============================================================================

def texto_sem_rotulos(elemento: Tag | None) -> str | None:
    """Texto de um elemento sem os rótulos acessíveis (<span>) e ícones (<svg>).

    Ex.: <h3><span class="sr-only">Quantidade de quartos </span><svg/>2</h3> → "2"
    Se, após remover os rótulos, não sobrar texto (estrutura mudou), devolve o
    texto completo — melhor um valor com rótulo do que um valor perdido.
    """
    if elemento is None:
        return None
    clone = copy.copy(elemento)                 # não altera a árvore original
    for filho in clone.find_all(TAGS_ROTULO):
        filho.decompose()
    texto = limpar_texto(clone.get_text(" ", strip=True))
    if texto:
        return texto
    return limpar_texto(elemento.get_text(" ", strip=True))


def extrair_preco_e_encargos(unidade: Tag) -> dict:
    """Aluguel, condomínio e IPTU a partir do bloco de preço.

    Texto típico do bloco (após limpeza):
        "R$ 3.800 / mês Cond. R$ 800 • IPTU R$ 104"
        "R$ 4.238 / mês Cond. não informado • IPTU não informado"
    Os valores ficam como texto ("R$ 800", "não informado"); a conversão é da Camada Prata.
    """
    resultado = {"valor_aluguel": None, "valor_condominio": None, "valor_iptu": None, "etiqueta_preco": None}
    try:
        bloco = unidade.select_one(SELETORES["bloco_preco"])
        if bloco is None:
            return resultado
        texto = limpar_texto(bloco.get_text(" ", strip=True)) or ""

        casamento = REGEX_VALOR_MONETARIO.search(texto)
        if casamento:
            resultado["valor_aluguel"] = limpar_texto(casamento.group(0))
        else:
            # Sem valor monetário (ex.: "Sob consulta"): guarda o texto da primeira linha
            primeiro_p = bloco.find("p")
            resultado["valor_aluguel"] = limpar_texto(primeiro_p.get_text(" ", strip=True) if primeiro_p else texto)

        resultado["valor_condominio"], _ = valor_apos_rotulo(texto, REGEX_ROTULO_CONDOMINIO)
        resultado["valor_iptu"], sobra = valor_apos_rotulo(texto, REGEX_ROTULO_IPTU)
        # O que vier depois do IPTU (ou do condomínio, se não houver IPTU) é a etiqueta
        if resultado["valor_iptu"] is None:
            _, sobra = valor_apos_rotulo(texto, REGEX_ROTULO_CONDOMINIO)
        resultado["etiqueta_preco"] = limpar_texto(re.sub(r"^[\s•|]+", "", sobra or ""))
    except Exception as erro:  # noqa: BLE001
        logging.debug("Falha ao extrair preço/encargos: %s", erro)
    return resultado


def valor_apos_rotulo(texto: str, rotulo: re.Pattern) -> tuple[str | None, str]:
    """Valor que segue um rótulo ("Cond.", "IPTU") no texto do bloco de preço, e o texto restante.

    Lê primeiro um valor/status conhecido ("R$ 800", "isento", "não informado"); só se
    não houver, cai para "tudo até o próximo separador". Assim uma etiqueta como
    "Ótimo preço" colada ao final não entra no valor.
    """
    encontrado = rotulo.search(texto)
    if not encontrado:
        return None, ""
    resto = texto[encontrado.end():]
    valor = REGEX_VALOR_OU_STATUS.match(resto)
    if valor:
        return limpar_texto(valor.group(0)), resto[valor.end():]
    generico = REGEX_ATE_SEPARADOR.match(resto)
    if generico:
        return limpar_texto(generico.group(1)), resto[generico.end():]
    return None, resto


def extrair_id_e_url(unidade: Tag) -> tuple[str | None, str | None]:
    """ID numérico e URL (sem query string) do anúncio, a partir do link para /imovel/..."""
    try:
        if unidade.name == "a" and unidade.get("href"):
            link = unidade
        else:
            link = unidade.select_one(SELETORES["link_anuncio"])
        if link is None or not link.get("href"):
            return None, None
        href = link["href"]
        if href.startswith("/"):
            href = BASE_URL + href
        partes = urlsplit(href)
        url_limpa = urlunsplit((partes.scheme, partes.netloc, partes.path, "", ""))
        casamento = REGEX_ID_ANUNCIO.search(partes.path)
        return (casamento.group(1) if casamento else None), url_limpa
    except Exception as erro:  # noqa: BLE001
        logging.debug("Falha ao extrair id/url: %s", erro)
        return None, None


def extrair_anuncios_agrupados(card: Tag) -> str | None:
    """Quantos anúncios o ZAP fundiu neste card ("Ver os 2 anúncios deste imóvel"); None nos cards comuns."""
    botao = card.select_one(SELETORES["botao_agrupado"])
    if botao is None:
        return None
    casamento = REGEX_ANUNCIOS_AGRUPADOS.search(botao.get_text(" ", strip=True))
    return casamento.group(1) if casamento else "sim"


def extrair_unidades_do_card(card: Tag) -> list[Tag]:
    """Lista de "unidades de anúncio" dentro de um card.

    Normalmente é o próprio card (1 anúncio). Cards agrupados de uma imobiliária
    trazem vários anúncios, cada um em um <a href=".../imovel/..."> com seu preço.
    """
    try:
        links = [a for a in card.select(SELETORES["link_anuncio"]) if a.select_one(SELETORES["bloco_preco"])]
        return links or [card]
    except Exception:  # noqa: BLE001
        return [card]


def extrair_anuncio(unidade: Tag, card: Tag, bairro_busca: str, pagina: int) -> dict | None:
    """Monta o registro de UM anúncio. Cada campo é extraído de forma independente."""
    registro: dict = {coluna: None for coluna in COLUNAS_OBRIGATORIAS + COLUNAS_RASTREABILIDADE}
    registro["bairro_busca"] = bairro_busca
    registro["pagina"] = pagina
    registro["data_coleta"] = datetime.now().isoformat(timespec="seconds")

    def tenta(nome_campo: str, extrator) -> None:
        """Executa o extrator; em caso de erro registra no log e deixa o campo vazio."""
        try:
            registro[nome_campo] = extrator()
        except Exception as erro:  # noqa: BLE001
            logging.debug("Campo '%s' não extraído: %s", nome_campo, erro)

    tenta("bairro",       lambda: texto_sem_rotulos(unidade.select_one(SELETORES["localizacao"])))
    tenta("rua",          lambda: texto_sem_rotulos(unidade.select_one(SELETORES["rua"])))
    tenta("area_util",    lambda: texto_sem_rotulos(unidade.select_one(SELETORES["area_util"])))
    tenta("quartos",      lambda: texto_sem_rotulos(unidade.select_one(SELETORES["quartos"])))
    tenta("tag_destaque", lambda: texto_sem_rotulos(unidade.select_one(SELETORES["tag_destaque"])))
    tenta("tipo_anuncio", lambda: limpar_texto(card.get("data-type")))
    tenta("anuncios_agrupados", lambda: extrair_anuncios_agrupados(card))
    registro.update(extrair_preco_e_encargos(unidade))
    registro["id_anuncio"], registro["url_anuncio"] = extrair_id_e_url(unidade)

    # Fragmento sem preço e sem localização não é um anúncio
    if not registro["valor_aluguel"] and not registro["bairro"]:
        return None
    return registro


def extrair_anuncios_da_pagina(html: str, bairro_busca: str, pagina: int) -> list[dict]:
    """HTML de uma página de resultados → lista de registros (um por anúncio)."""
    soup = BeautifulSoup(html, PARSER_HTML)

    # Remove blocos que não são resultados da busca (ex.: carrossel de recomendações)
    for bloco in soup.select(SELETORES["blocos_ignorados"]):
        bloco.decompose()

    registros: list[dict] = []
    for card in soup.select(SELETORES["card"]):
        try:
            for unidade in extrair_unidades_do_card(card):
                registro = extrair_anuncio(unidade, card, bairro_busca, pagina)
                if registro:
                    registros.append(registro)
        except Exception as erro:  # noqa: BLE001 — um card problemático nunca derruba a página
            logging.warning("Card ignorado por erro inesperado: %s", erro)
    return registros


def total_de_resultados(html: str) -> int | None:
    """Lê o total informado pelo site ("1.122 Imóveis para alugar em ..."), só para o log."""
    try:
        titulo = BeautifulSoup(html, PARSER_HTML).select_one(SELETORES["titulo_busca"])
        casamento = re.match(r"\s*([\d.]+)", titulo.get_text(strip=True)) if titulo else None
        return int(casamento.group(1).replace(".", "")) if casamento else None
    except Exception:  # noqa: BLE001
        return None


def chave_de_deduplicacao(registro: dict) -> str | None:
    """id_anuncio → url_anuncio → (para cards agrupados, que não têm nenhum dos dois)
    uma chave composta pelos campos do anúncio."""
    if registro.get("id_anuncio"):
        return f"id:{registro['id_anuncio']}"
    if registro.get("url_anuncio"):
        return f"url:{registro['url_anuncio']}"
    campos = [registro.get(c) or "" for c in ("bairro", "rua", "valor_aluguel", "valor_condominio", "area_util", "quartos")]
    return "campos:" + "|".join(campos) if any(campos) else None


def filtrar_novos(registros: list[dict], ids_vistos: set[str]) -> list[dict]:
    """Descarta anúncios já coletados nesta execução (ver chave_de_deduplicacao)."""
    if not DEDUPLICAR_POR_ID:
        return list(registros)
    novos: list[dict] = []
    for registro in registros:
        chave = chave_de_deduplicacao(registro)
        if chave:
            if chave in ids_vistos:
                continue
            ids_vistos.add(chave)
        novos.append(registro)
    return novos


# =============================================================================
# 5. ORQUESTRAÇÃO
# =============================================================================

def montar_url(slug: str, pagina: int) -> str:
    """URL da página de resultados já com a ordenação "Mais recente" aplicada."""
    parametros = dict(PARAMETROS_URL_FIXOS)
    if pagina > 1:
        parametros[PARAMETRO_PAGINA] = pagina
    return f"{BASE_URL}/{TRANSACAO}/imoveis/{slug}/?{urlencode(parametros)}"


# Estado da gravação: se o CSV principal estiver travado (aberto no Excel), os
# checkpoints vão para UMA cópia de emergência por execução, sempre a mesma.
_GRAVACAO = {"copia_emergencia": None, "usando_copia": False}


def salvar_csv(registros: list[dict], caminho: str | Path = ARQUIVO_SAIDA) -> Path:
    """Grava (sobrescreve) o CSV COMPLETO. Chamado após cada página → checkpoint contra quedas.

    Cada gravação contém todos os anúncios coletados até o momento, então o arquivo
    mais recente é sempre o dataset inteiro — nunca é preciso juntar arquivos.
    """
    colunas = COLUNAS_OBRIGATORIAS + (COLUNAS_RASTREABILIDADE if INCLUIR_COLUNAS_RASTREABILIDADE else [])
    df = pd.DataFrame(registros, columns=colunas)
    destino = Path(caminho)
    try:
        df.to_csv(destino, index=False, sep=SEPARADOR_CSV, encoding=CODIFICACAO_CSV)
    except PermissionError:
        # Acontece quando o CSV está aberto no Excel (o Windows tranca o arquivo).
        if _GRAVACAO["copia_emergencia"] is None:
            _GRAVACAO["copia_emergencia"] = destino.with_name(
                f"{destino.stem}_execucao_{datetime.now():%Y%m%d_%H%M%S}{destino.suffix}")
        copia = _GRAVACAO["copia_emergencia"]
        df.to_csv(copia, index=False, sep=SEPARADOR_CSV, encoding=CODIFICACAO_CSV)
        if not _GRAVACAO["usando_copia"]:
            logging.error("Sem permissão para gravar %s (está aberto no Excel?). Enquanto isso os checkpoints "
                          "vão para %s — feche o arquivo e a gravação volta ao normal sozinha.", destino, copia.name)
            _GRAVACAO["usando_copia"] = True
        return copia
    if _GRAVACAO["usando_copia"]:
        logging.info("Gravação em %s restabelecida.", destino)
        _GRAVACAO["usando_copia"] = False
    return destino


def localizar_csv_mais_recente(arquivo_saida: str | Path) -> Path | None:
    """O CSV principal ou, se houver cópias de emergência mais novas que ele, a mais recente delas."""
    principal = Path(arquivo_saida)
    candidatos = [principal] if principal.exists() else []
    candidatos += [c for c in principal.parent.glob(f"{principal.stem}_*{principal.suffix}")
                   if "_anterior_" not in c.name]
    if not candidatos:
        return None
    return max(candidatos, key=lambda c: c.stat().st_mtime)


def consolidar_copias(arquivo_saida: str | Path) -> int:
    """--consolidar: promove a cópia mais recente a CSV principal e apaga as cópias antigas."""
    principal = Path(arquivo_saida)
    mais_recente = localizar_csv_mais_recente(principal)
    if mais_recente is None:
        logging.info("Nenhum CSV encontrado para consolidar.")
        return 0
    if mais_recente != principal:
        try:
            shutil.copyfile(mais_recente, principal)
            logging.info("%s promovido a %s.", mais_recente.name, principal.name)
        except PermissionError:
            logging.error("%s está aberto em outro programa (Excel?). Feche-o e rode --consolidar de novo.", principal)
            return 1
    copias = [c for c in principal.parent.glob(f"{principal.stem}_*{principal.suffix}") if "_anterior_" not in c.name]
    removidas = 0
    for copia in copias:
        try:
            copia.unlink()
            removidas += 1
        except OSError as erro:
            logging.warning("Não foi possível apagar %s: %s", copia.name, erro)
    linhas = sum(1 for _ in principal.open(encoding=CODIFICACAO_CSV)) - 1
    logging.info("Consolidado: %s com %d anúncios; %d cópia(s) de emergência apagada(s).", principal.name, linhas, removidas)
    return 0


def raspar_bairro(driver: uc.Chrome, bairro: dict, max_paginas: int, ids_vistos: set[str],
                  registros_totais: list[dict], estado: dict, arquivo_saida: str,
                  pagina_inicial: int = 1) -> tuple[str, int]:
    """Percorre as páginas de um bairro (a partir de `pagina_inicial`) até o limite ou o fim dos resultados.

    Retorna ("concluido", n) quando chegou ao fim dos resultados/limite, ou ("abortado", n)
    quando uma página não carregou nem depois de repetida — nesse caso o bairro NÃO é
    marcado como concluído, para ser refeito em --continuar. Nenhuma página é pulada.
    Levanta NavegadorTravado (com a página em que parou) quando o Chrome para de responder.
    """
    nome, slug = bairro["nome"], bairro["slug"]
    coletados = 0
    ids_pagina_anterior: set[str] = set()
    logging.info("=" * 70)
    logging.info("BAIRRO: %s  (limite: %d páginas%s)", nome, max_paginas,
                 f", retomando da página {pagina_inicial}" if pagina_inicial > 1 else "")

    for pagina in range(pagina_inicial, max_paginas + 1):
        url = montar_url(slug, pagina)
        logging.info("[%s] Página %d/%d → %s", nome, pagina, max_paginas, url)

        try:
            html = carregar_pagina(driver, url, esperar_cookies=not estado["cookies_verificados"])
            estado["cookies_verificados"] = True
            if html is None:
                # Segunda chance para a MESMA página, depois de uma pausa maior
                logging.warning("[%s] Página %d não carregou após %d tentativas; nova rodada após pausa.",
                                nome, pagina, TENTATIVAS_POR_PAGINA)
                espera_aleatoria(ESPERA_APOS_FALHA, "(após falha)")
                html = carregar_pagina(driver, url, esperar_cookies=False)
        except NavegadorTravado as erro:
            erro.pagina = pagina          # permite retomar desta página após reiniciar o Chrome
            raise
        if html is None:
            logging.error("[%s] Página %d não carregou — bairro abortado (será refeito em --continuar).", nome, pagina)
            return "abortado", coletados

        if pagina == 1:
            total = total_de_resultados(html)
            if total is not None:
                logging.info("[%s] O site informa %s imóveis (~%d páginas).",
                             nome, f"{total:,}".replace(",", "."), -(-total // 30))

        # --- Parsing ---
        registros_pagina = extrair_anuncios_da_pagina(html, nome, pagina)
        if not registros_pagina:
            logging.info("[%s] Nenhum card na página %d — fim dos resultados do bairro.", nome, pagina)
            break

        novos = filtrar_novos(registros_pagina, ids_vistos)
        sem_condominio = sum(1 for r in novos if not r["valor_condominio"])
        sem_quartos = sum(1 for r in novos if not r["quartos"])
        logging.info("[%s] Página %d: %d anúncios no HTML, %d novos, %d repetidos "
                     "(sem condomínio: %d, sem quartos: %d).",
                     nome, pagina, len(registros_pagina), len(novos),
                     len(registros_pagina) - len(novos), sem_condominio, sem_quartos)

        # Fim dos resultados: quando `pagina` passa do fim, o site devolve a mesma página
        # de novo. Só isso encerra o bairro — "0 novos" sozinho NÃO encerra, porque os
        # anúncios podem já ter vindo de outro bairro (vizinhos) ou de uma execução anterior.
        ids_pagina = {chave_de_deduplicacao(r) for r in registros_pagina} - {None}
        if pagina > 1 and ids_pagina and ids_pagina == ids_pagina_anterior:
            logging.info("[%s] Página %d repete a página %d — fim dos resultados do bairro.", nome, pagina, pagina - 1)
            break
        ids_pagina_anterior = ids_pagina

        registros_totais.extend(novos)
        coletados += len(novos)
        salvar_csv(registros_totais, arquivo_saida)   # checkpoint
        estado["em_andamento"] = {"bairro": nome, "ultima_pagina": pagina}
        salvar_progresso(ARQUIVO_PROGRESSO, estado.get("concluidos", []), arquivo_saida, estado["em_andamento"])

        if pagina < max_paginas:
            espera_aleatoria(ESPERA_ENTRE_PAGINAS, "(entre páginas)")

    logging.info("[%s] Concluído: %d anúncios novos coletados.", nome, coletados)
    return "concluido", coletados


def interpretar_argumentos(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Camada Bronze — coleta de anúncios de aluguel do ZAP Imóveis (RJ).",
        epilog="Coleta completa do município: python zap_scraper.py --todos   "
               "(retome depois de uma interrupção com --todos --continuar)")
    parser.add_argument("--todos", action="store_true",
                        help=f"usa todos os bairros de {NOME_BAIRROS} em vez da lista BAIRROS")
    parser.add_argument("--arquivo-bairros", default=ARQUIVO_BAIRROS, metavar="CSV",
                        help=f"arquivo de bairros usado com --todos (padrão: {NOME_BAIRROS})")
    parser.add_argument("--zonas", nargs="*", metavar="ZONA",
                        help="restringe a coleta a estas zonas: zona-sul zona-norte zona-oeste zona-central")
    parser.add_argument("--bairros", nargs="*", metavar="NOME",
                        help="restringe a coleta a estes bairros (pelo nome)")
    parser.add_argument("--continuar", action="store_true",
                        help="retoma uma coleta interrompida: reaproveita o CSV existente e pula os bairros concluídos")
    parser.add_argument("--refazer", nargs="*", metavar="NOME",
                        help="com --continuar: coleta de novo estes bairros mesmo que constem como concluídos "
                             "(os anúncios já gravados são mantidos; só os novos entram)")
    parser.add_argument("--consolidar", action="store_true",
                        help="não coleta: promove a cópia de emergência mais recente a CSV principal e apaga as demais")
    parser.add_argument("--max-paginas", type=int, default=MAX_PAGINAS_POR_BAIRRO,
                        help=f"limite de páginas por bairro (padrão: {MAX_PAGINAS_POR_BAIRRO})")
    parser.add_argument("--teste", action="store_true", help="modo rápido: 1 página por bairro")
    parser.add_argument("--saida", default=ARQUIVO_SAIDA, help=f"arquivo CSV de saída (padrão: {Path(ARQUIVO_SAIDA).name})")
    return parser.parse_args(argv)


def normalizar_nome(texto: str) -> str:
    """"São Cristóvão" → "sao cristovao": comparação sem acentos nem maiúsculas."""
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower().strip()


def carregar_bairros_do_arquivo(caminho: str | Path) -> list[dict]:
    """Lê o CSV de bairros (colunas nome, zona, slug; as demais são informativas)."""
    caminho = Path(caminho)
    if not caminho.exists():                             # nome solto: procura nos lugares usuais
        for pasta in (PASTA_ENTRADA, PASTA):
            if (pasta / caminho).exists():
                caminho = pasta / caminho
                break
    if not caminho.exists():
        raise FileNotFoundError(f"Arquivo de bairros não encontrado: {caminho.resolve()}")
    with caminho.open(encoding="utf-8-sig", newline="") as arquivo:
        linhas = list(csv.DictReader(arquivo))
    bairros = [{"nome": l["nome"].strip(), "zona": (l.get("zona") or "").strip(), "slug": l["slug"].strip()}
               for l in linhas if l.get("nome") and l.get("slug")]
    if not bairros:
        raise ValueError(f"Nenhum bairro válido em {caminho} (esperadas as colunas nome, zona, slug).")
    return bairros


def selecionar_bairros(lista: list[dict], nomes: list[str] | None = None,
                       zonas: list[str] | None = None) -> list[dict]:
    """Filtra a lista de bairros por nomes e/ou zonas (sem diferenciar maiúsculas/acentos)."""
    selecionados = list(lista)
    if zonas:
        desejadas = {normalizar_nome(z) for z in zonas}
        selecionados = [b for b in selecionados if normalizar_nome(b.get("zona", "")) in desejadas]
        desconhecidas = desejadas - {normalizar_nome(b.get("zona", "")) for b in lista}
        if desconhecidas:
            logging.warning("Zonas não encontradas: %s (válidas: %s)", ", ".join(sorted(desconhecidas)),
                            ", ".join(sorted({b.get("zona", "") for b in lista if b.get("zona")})))
    if nomes:
        desejados = {normalizar_nome(n) for n in nomes}
        selecionados = [b for b in selecionados if normalizar_nome(b["nome"]) in desejados]
        desconhecidos = desejados - {normalizar_nome(b["nome"]) for b in selecionados}
        if desconhecidos:
            logging.warning("Bairros não encontrados na lista: %s", ", ".join(sorted(desconhecidos)))
    return selecionados


def carregar_estado_anterior(arquivo_saida: str | Path, arquivo_progresso: str | Path) -> tuple[list[dict], set[str], list[str], dict | None]:
    """Para --continuar: registros já gravados no CSV, suas chaves de deduplicação, os bairros concluídos
    e o bairro que estava em andamento (com a última página gravada)."""
    registros: list[dict] = []
    ids_vistos: set[str] = set()
    concluidos: list[str] = []
    em_andamento: dict | None = None

    caminho_csv = localizar_csv_mais_recente(arquivo_saida)
    if caminho_csv is not None:
        if caminho_csv != Path(arquivo_saida):
            logging.warning("O CSV principal está desatualizado; retomando a partir da cópia de emergência mais recente: %s", caminho_csv.name)
        df = pd.read_csv(caminho_csv, sep=SEPARADOR_CSV, encoding=CODIFICACAO_CSV, dtype=str, keep_default_na=False)
        for linha in df.to_dict("records"):
            registro = {coluna: (valor if valor != "" else None) for coluna, valor in linha.items()}
            registros.append(registro)
            chave = chave_de_deduplicacao(registro)
            if chave:
                ids_vistos.add(chave)
        logging.info("Retomando: %d anúncios já gravados em %s.", len(registros), caminho_csv)

    caminho_progresso = Path(arquivo_progresso)
    if caminho_progresso.exists():
        try:
            dados = json.loads(caminho_progresso.read_text(encoding="utf-8"))
            concluidos = [str(b) for b in dados.get("bairros_concluidos", [])]
            em_andamento = dados.get("em_andamento") or None
            logging.info("Retomando: %d bairro(s) já concluído(s) segundo %s.", len(concluidos), caminho_progresso)
            if em_andamento:
                logging.info("Retomando: %s estava em andamento (última página gravada: %s).",
                             em_andamento.get("bairro"), em_andamento.get("ultima_pagina"))
        except (ValueError, OSError) as erro:
            logging.warning("Não foi possível ler %s (%s) — nenhum bairro será pulado.", caminho_progresso, erro)
    return registros, ids_vistos, concluidos, em_andamento


def salvar_progresso(arquivo_progresso: str | Path, concluidos: list[str], arquivo_saida: str | Path,
                     em_andamento: dict | None = None) -> None:
    """Grava os bairros concluídos e o bairro/página em andamento (lidos por --continuar)."""
    dados = {
        "arquivo_saida": str(arquivo_saida),
        "bairros_concluidos": concluidos,
        "em_andamento": em_andamento,          # {"bairro": nome, "ultima_pagina": n} ou None
        "atualizado_em": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        Path(arquivo_progresso).write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as erro:
        logging.warning("Não foi possível gravar o progresso em %s: %s", arquivo_progresso, erro)


def preservar_saida_anterior(arquivo_saida: str | Path) -> None:
    """Sem --continuar, um CSV antigo seria sobrescrito: renomeia-o com data/hora para não perder dados."""
    caminho = Path(arquivo_saida)
    if caminho.exists() and caminho.stat().st_size > 0:
        backup = caminho.with_name(f"{caminho.stem}_anterior_{datetime.fromtimestamp(caminho.stat().st_mtime):%Y%m%d_%H%M%S}{caminho.suffix}")
        try:
            caminho.rename(backup)
            logging.info("CSV anterior preservado como %s (use --continuar para retomar em vez de recomeçar).", backup.name)
        except OSError as erro:
            logging.warning("Não foi possível renomear o CSV anterior (%s). Se ele estiver aberto no Excel, feche-o: "
                            "enquanto estiver travado, os checkpoints irão para uma cópia de emergência.", erro)


def main(argv: list[str] | None = None) -> int:
    args = interpretar_argumentos(argv)
    configurar_logging()
    if args.consolidar:
        return consolidar_copias(args.saida)

    # --- Lista de bairros ---
    try:
        lista = carregar_bairros_do_arquivo(args.arquivo_bairros) if args.todos else BAIRROS
    except (FileNotFoundError, ValueError) as erro:
        logging.error("%s", erro)
        return 1
    bairros = selecionar_bairros(lista, args.bairros, args.zonas)
    if not bairros:
        logging.error("Nenhum bairro para coletar. Verifique a lista BAIRROS / %s e os argumentos --bairros/--zonas.", args.arquivo_bairros)
        return 1
    max_paginas = 1 if args.teste else max(1, args.max_paginas)

    # --- Estado inicial (novo ou retomado) ---
    registros: list[dict] = []
    ids_vistos: set[str] = set()
    concluidos: list[str] = []
    em_andamento: dict | None = None
    if args.refazer and not args.continuar:
        args.continuar = True                 # refazer só faz sentido preservando o que já existe
    if args.continuar:
        registros, ids_vistos, concluidos, em_andamento = carregar_estado_anterior(args.saida, ARQUIVO_PROGRESSO)
    else:
        preservar_saida_anterior(args.saida)
        salvar_progresso(ARQUIVO_PROGRESSO, [], args.saida)   # zera o progresso de execuções anteriores
    refazer = {normalizar_nome(n) for n in (args.refazer or [])}
    if refazer:
        bairros = [b for b in bairros if normalizar_nome(b["nome"]) in refazer]
        concluidos = [c for c in concluidos if normalizar_nome(c) not in refazer]
        em_andamento = None
        logging.info("Refazendo: %s", ", ".join(b["nome"] for b in bairros) or "(nenhum bairro encontrado)")
    pendentes = [b for b in bairros if b["nome"] not in concluidos]
    if not pendentes:
        logging.info("Todos os %d bairros selecionados já constam como concluídos em %s. Nada a fazer.", len(bairros), ARQUIVO_PROGRESSO)
        return 0

    estado = {"cookies_verificados": False, "concluidos": concluidos, "em_andamento": None}
    driver = None
    inicio = time.time()
    logging.info("Iniciando coleta | %d bairro(s) pendente(s) de %d selecionado(s) | até %d página(s) por bairro | ordenação: %s",
                 len(pendentes), len(bairros), max_paginas, PARAMETROS_URL_FIXOS)
    logging.info("Bairros: %s", ", ".join(b["nome"] for b in pendentes))

    abortados_seguidos = 0
    try:
        driver = iniciar_navegador()
        for indice, bairro in enumerate(pendentes, start=1):
            # Reinício preventivo do Chrome em coletas longas
            if indice > 1 and (indice - 1) % REINICIAR_NAVEGADOR_A_CADA == 0:
                driver = reiniciar_navegador(driver, estado, f"preventivo, a cada {REINICIAR_NAVEGADOR_A_CADA} bairros")

            # Retomada de um bairro interrompido no meio: continua da página seguinte à última gravada
            pagina_inicial = 1
            if em_andamento and em_andamento.get("bairro") == bairro["nome"]:
                pagina_inicial = int(em_andamento.get("ultima_pagina") or 0) + 1
                em_andamento = None

            # Coleta do bairro, com até 2 reinícios do navegador se ele travar (retomando da página em que parou)
            status = None
            for tentativa in range(1, 4):
                try:
                    status, _ = raspar_bairro(driver, bairro, max_paginas, ids_vistos, registros, estado, args.saida,
                                              pagina_inicial=pagina_inicial)
                    break
                except NavegadorTravado as erro:
                    if tentativa == 3:
                        raise RuntimeError(f"o navegador travou 3 vezes seguidas em {bairro['nome']}") from erro
                    pagina_inicial = erro.pagina or pagina_inicial
                    logging.warning("Navegador travou (%s). Reiniciando e repetindo %s da página %d (tentativa %d/3).",
                                    erro, bairro["nome"], pagina_inicial, tentativa + 1)
                    driver = reiniciar_navegador(driver, estado, "travamento")
                    espera_aleatoria(ESPERA_APOS_FALHA, "(após reinício)")

            if status == "abortado":
                abortados_seguidos += 1
                if abortados_seguidos >= MAX_BAIRROS_ABORTADOS_SEGUIDOS:
                    logging.error("%d bairros seguidos sem conseguir carregar — provável bloqueio ou queda de rede. "
                                  "Encerrando; aguarde um tempo e retome com --continuar.", abortados_seguidos)
                    break
                espera_aleatoria(ESPERA_APOS_FALHA, "(após bairro abortado)")
                continue
            abortados_seguidos = 0

            concluidos.append(bairro["nome"])
            estado["em_andamento"] = None
            salvar_progresso(ARQUIVO_PROGRESSO, concluidos, args.saida)

            decorrido = time.time() - inicio
            restantes = len(pendentes) - indice
            logging.info("Progresso: %d/%d bairros | %d anúncios no total | %.0f min decorridos | ~%.0f min restantes (estimativa).",
                         indice, len(pendentes), len(registros), decorrido / 60, decorrido / indice * restantes / 60)
            if restantes:
                espera_aleatoria(ESPERA_ENTRE_BAIRROS, "(troca de bairro)")
    except KeyboardInterrupt:
        logging.warning("Interrompido pelo usuário — salvando o que foi coletado até aqui. Retome com --continuar.")
    except Exception:  # noqa: BLE001
        logging.exception("Erro fatal na execução — salvando o que foi coletado até aqui. Retome com --continuar.")
    finally:
        caminho = salvar_csv(registros, args.saida)
        encerrar_navegador(driver)

    logging.info("Coleta finalizada: %d anúncios gravados em %s (%.1f min).",
                 len(registros), caminho.resolve(), (time.time() - inicio) / 60)
    return 0


if __name__ == "__main__":
    # O guard é obrigatório: o undetected-chromedriver usa subprocesso e, no Windows,
    # sem ele o script seria reimportado e o Chrome aberto duas vezes.
    sys.exit(main())
