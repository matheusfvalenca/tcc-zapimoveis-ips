# -*- coding: utf-8 -*-
"""
Testes da camada de parsing — rodam OFFLINE, sem abrir o navegador.

    python -m pytest tests/ -v          (com pytest instalado)
    python tests/test_parser.py         (sem pytest: usa asserts simples)

O HTML de entrada é um recorte real da página de resultados do ZAP Imóveis
(tests/fixtures/pagina_exemplo.html). Se o site mudar e os seletores forem
ajustados, atualize o fixture com um novo recorte e rode os testes de novo.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Permite importar zap_scraper.py a partir da pasta da sua camada
RAIZ = Path(__file__).resolve().parent.parent
# o script fica na pasta da sua camada; se o projeto estiver todo numa pasta só,
# a raiz continua servindo
for _pasta in (RAIZ / "1_bronze", RAIZ):
    sys.path.insert(0, str(_pasta))

import pandas as pd  # noqa: E402

import zap_scraper as zs  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "pagina_exemplo.html"


def carregar_registros() -> list[dict]:
    html = FIXTURE.read_text(encoding="utf-8")
    return zs.extrair_anuncios_da_pagina(html, bairro_busca="Copacabana", pagina=1)


def por_id(registros: list[dict]) -> dict[str, dict]:
    return {r["id_anuncio"]: r for r in registros}


def test_quantidade_de_anuncios_extraidos():
    registros = carregar_registros()
    # 1 + 1 + 1 + 4 (card agrupado renderiza cada anúncio 2x) + 1 (deduplicado) + 1 (etiqueta) = 9;
    # recomendação e card quebrado ficam de fora
    assert len(registros) == 9, [r["id_anuncio"] for r in registros]
    assert "9990000009" not in por_id(registros), "card do carrossel de recomendações não pode entrar"


def test_card_completo():
    r = por_id(carregar_registros())["2901304295"]
    assert r["bairro"] == "Copacabana, Rio de Janeiro"          # sem o rótulo "Apartamento para alugar com..."
    assert r["valor_aluguel"] == "R$ 7.500"                      # &nbsp; virou espaço comum
    assert r["valor_condominio"] == "R$ 1.200"
    assert r["valor_iptu"] == "R$ 292"
    assert r["area_util"] == "109 m²"                            # sem o rótulo "Tamanho do imóvel"
    assert r["quartos"] == "2"                                   # sem o rótulo "Quantidade de quartos"
    assert r["rua"] == "Avenida Nossa Senhora de Copacabana"
    assert r["tipo_anuncio"] == "SUPER PREMIUM"
    assert r["tag_destaque"] == "Super Destaque"
    assert r["url_anuncio"].endswith("-id-2901304295/")          # query string removida
    assert "?" not in r["url_anuncio"]
    assert r["bairro_busca"] == "Copacabana" and r["pagina"] == 1 and r["data_coleta"]


def test_card_sem_quartos_preserva_os_demais_campos():
    r = por_id(carregar_registros())["2909870344"]
    assert r["quartos"] is None
    assert r["valor_aluguel"] == "R$ 2.500"
    assert r["valor_condominio"] == "R$ 1.600"
    assert r["area_util"] == "40 m²"
    assert r["tipo_anuncio"] is None                             # card orgânico: sem data-type
    assert r["tag_destaque"] == "Destaque"


def test_condominio_nao_informado_vem_como_texto():
    r = por_id(carregar_registros())["1000000003"]
    assert r["valor_aluguel"] == "R$ 4.238"
    assert r["valor_condominio"] == "não informado"
    assert r["valor_iptu"] == "não informado"                 # a etiqueta que vem depois NÃO entra no IPTU
    assert r["etiqueta_preco"] == "Ótimo preço"
    assert r["bairro"] == "Leme, Rio de Janeiro"                 # bairro vizinho dentro da busca de Copacabana
    assert r["url_anuncio"].startswith("https://www.zapimoveis.com.br/imovel/")  # href relativo → absoluto


def test_card_agrupado_gera_um_registro_por_anuncio_e_deduplica():
    registros = carregar_registros()
    agrupados = [r for r in registros if r["tipo_anuncio"] == "FIXED TOP"]
    assert len(agrupados) == 4                                   # 2 anúncios x 2 renderizações
    novos = zs.filtrar_novos(registros, ids_vistos=set())
    assert len(novos) == 7                                       # 9 - 2 duplicatas do card agrupado
    ids = [r["id_anuncio"] for r in novos]
    assert ids.count("2870000001") == 1 and ids.count("2870000002") == 1
    r = por_id(novos)["2870000001"]
    assert r["valor_aluguel"] == "R$ 15.000" and r["valor_condominio"] == "R$ 8.474" and r["area_util"] == "409 m²"


def test_card_deduplicado_sem_link_e_com_faixa_de_area():
    registros = carregar_registros()
    [r] = [r for r in registros if r["anuncios_agrupados"]]
    assert r["anuncios_agrupados"] == "2"
    assert r["id_anuncio"] is None and r["url_anuncio"] is None   # o card não tem link
    assert r["area_util"] == "130 - 135 m²"                       # faixa preservada como texto
    assert r["valor_aluguel"] == "R$ 6.500" and r["quartos"] == "3"
    assert zs.chave_de_deduplicacao(r).startswith("campos:Copacabana, Rio de Janeiro|Rua Domingos Ferreira|R$ 6.500")


def test_condominio_isento_iptu_em_reais_e_etiqueta():
    r = por_id(carregar_registros())["1000000008"]
    assert r["valor_condominio"] == "isento"
    assert r["valor_iptu"] == "R$ 90"
    assert r["etiqueta_preco"] == "Baixou de preço"
    assert r["quartos"] is None and r["area_util"] == "25 m²"
    completo = por_id(carregar_registros())["2901304295"]
    assert completo["etiqueta_preco"] is None                   # sem etiqueta → vazio


def test_deduplicacao_entre_paginas():
    ids_vistos: set[str] = set()
    pagina1 = zs.filtrar_novos(carregar_registros(), ids_vistos)
    pagina2 = zs.filtrar_novos(carregar_registros(), ids_vistos)  # mesma página repetida pelo site
    assert len(pagina1) == 7 and len(pagina2) == 0                # inclui o card sem id (chave composta)


def test_total_de_resultados_e_url():
    html = FIXTURE.read_text(encoding="utf-8")
    assert zs.total_de_resultados(html) == 1122
    assert zs.montar_url("rj+rio-de-janeiro+zona-sul+urca", 1) == \
        "https://www.zapimoveis.com.br/aluguel/imoveis/rj+rio-de-janeiro+zona-sul+urca/?ordem=MOST_RECENT"
    assert zs.montar_url("rj+rio-de-janeiro+zona-sul+urca", 3).endswith("?ordem=MOST_RECENT&pagina=3")


def test_csv_tem_as_colunas_obrigatorias_na_ordem(tmp_path=None):
    destino = (tmp_path or Path(__file__).parent) / "_saida_teste.csv"
    novos = zs.filtrar_novos(carregar_registros(), set())
    try:
        zs.salvar_csv(novos, destino)
        df = pd.read_csv(destino, sep=zs.SEPARADOR_CSV, encoding=zs.CODIFICACAO_CSV, dtype=str)
        assert list(df.columns)[:5] == ["bairro", "valor_aluguel", "valor_condominio", "area_util", "quartos"]
        assert len(df) == 7
        assert df.loc[df["id_anuncio"] == "2909870344", "quartos"].isna().all()   # célula vazia, não "None"
    finally:
        destino.unlink(missing_ok=True)


def test_arquivo_de_bairros_completo():
    # usa o mesmo caminho que o coletor usa (0_entrada/bairros_rj.csv, ou a pasta do script
    # se o projeto estiver todo numa pasta só), em vez de supor onde o arquivo está
    bairros = zs.carregar_bairros_do_arquivo(Path(zs.ARQUIVO_BAIRROS))
    assert len(bairros) == 162
    slugs = [b["slug"] for b in bairros]
    assert len(set(slugs)) == len(slugs)                                  # sem repetição
    assert all(s.startswith("rj+rio-de-janeiro+zona-") and s.count("+") == 3 for s in slugs)
    assert {b["zona"] for b in bairros} == {"zona-sul", "zona-norte", "zona-oeste", "zona-central"}
    assert len(zs.selecionar_bairros(bairros, zonas=["zona-sul"])) == 20
    assert [b["nome"] for b in zs.selecionar_bairros(bairros, nomes=["sao cristovao", "MÉIER"])] == ["Méier", "São Cristóvão"]
    assert zs.montar_url(zs.selecionar_bairros(bairros, nomes=["Urca"])[0]["slug"], 1) == \
        "https://www.zapimoveis.com.br/aluguel/imoveis/rj+rio-de-janeiro+zona-sul+urca/?ordem=MOST_RECENT"


def test_limpar_texto():
    assert zs.limpar_texto("  R$\xa03.800 \n /mês ") == "R$ 3.800 /mês"
    assert zs.limpar_texto("") is None and zs.limpar_texto(None) is None


if __name__ == "__main__":
    # Execução sem pytest
    testes = [obj for nome, obj in sorted(globals().items()) if nome.startswith("test_") and callable(obj)]
    for teste in testes:
        teste()
        print(f"OK  {teste.__name__}")
    print(f"\n{len(testes)} testes passaram.")
