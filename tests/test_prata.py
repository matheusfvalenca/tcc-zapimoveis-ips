# -*- coding: utf-8 -*-
"""
Testes das funções de conversão e normalização da Camada Prata (prata_limpeza.py).

    python -m pytest tests/test_prata.py -v
    python tests/test_prata.py            (sem pytest)

Rodam em memória, sobre os formatos reais observados na base de 06-07/09/2026.
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
# o script fica na pasta da sua camada; se o projeto estiver todo numa pasta só,
# a raiz continua servindo
for _pasta in (RAIZ / "2_prata", RAIZ):
    sys.path.insert(0, str(_pasta))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import prata_limpeza as pl  # noqa: E402


def test_normalizar_chave():
    assert pl.normalizar_chave("São Cristóvão") == "sao cristovao"
    assert pl.normalizar_chave("  MÉIER ") == "meier"
    assert pl.normalizar_chave("Freguesia (Jacarepaguá)") == "freguesia jacarepagua"
    # o sublinhado conta como separador: nomes de coluna de arquivos diferentes se equivalem
    assert pl.normalizar_chave("regiao_administrativa") == pl.normalizar_chave("Região Administrativa")
    assert pl.normalizar_chave("IPS Geral") == pl.normalizar_chave("ips_geral")


def test_para_numero_valores_e_status():
    assert pl.para_numero("R$ 3.800") == 3800.0
    assert pl.para_numero("R$ 1.300.000") == 1300000.0
    assert pl.para_numero("isento") == 0.0            # isento é zero, não ausente
    assert np.isnan(pl.para_numero("não informado"))  # ausente é NaN, não zero
    assert np.isnan(pl.para_numero(""))
    # etiqueta promocional colada ao valor (artefato da coleta de 06/09)
    assert pl.para_numero("R$ 104 Ótimo preço") == 104.0
    assert pl.para_numero("isento Baixou de preço") == 0.0
    assert np.isnan(pl.para_numero("não informado Aluguel sem fiador"))


def test_extrair_etiqueta():
    assert pl.extrair_etiqueta("R$ 104 Ótimo preço") == "Ótimo preço"
    assert pl.extrair_etiqueta("isento Baixou de preço") == "Baixou de preço"
    assert pl.extrair_etiqueta("R$ 104") == ""


def test_area_para_numero():
    assert pl.area_para_numero("36 m²") == (36.0, False)
    assert pl.area_para_numero("130 - 135 m²") == (132.5, True)   # faixa → ponto médio, marcada
    assert pl.area_para_numero("")[0] != pl.area_para_numero("")[0]  # NaN


def test_classificar_imovel():
    base = "https://www.zapimoveis.com.br/imovel/aluguel-"
    assert pl.classificar_imovel(base + "apartamento-2-quartos-tijuca-id-1/") == ("Apartamento", "residencial")
    assert pl.classificar_imovel(base + "conjunto-comercial-sala-centro-id-2/")[1] == "comercial"
    assert pl.classificar_imovel(base + "galpao-deposito-armazem-centro-id-3/")[1] == "comercial"
    assert pl.classificar_imovel(base + "casa-de-condominio-4-quartos-id-4/") == ("Casa de condomínio", "residencial")
    assert pl.classificar_imovel("") == ("Não identificado", "não identificado")


def _base_exemplo() -> pd.DataFrame:
    """Pequena base bruta sintética, com os casos-limite reais."""
    return pd.DataFrame([
        # dois anúncios do MESMO imóvel (ids diferentes) → um deve sair na deduplicação
        {"bairro": "Copacabana, Rio de Janeiro", "valor_aluguel": "R$ 3.800", "valor_condominio": "R$ 800",
         "area_util": "36 m²", "quartos": "1", "bairro_busca": "Copacabana", "rua": "Rua Domingos Ferreira",
         "valor_iptu": "R$ 104", "tag_destaque": "", "etiqueta_preco": "", "id_anuncio": "1",
         "url_anuncio": "https://www.zapimoveis.com.br/imovel/aluguel-apartamento-1-quarto-id-1/", "pagina": "1",
         "data_coleta": "2026-09-06T15:00:00"},
        {"bairro": "Copacabana, Rio de Janeiro", "valor_aluguel": "R$ 3.800", "valor_condominio": "R$ 800",
         "area_util": "36 m²", "quartos": "1", "bairro_busca": "Copacabana", "rua": "Rua Domingos Ferreira",
         "valor_iptu": "R$ 104", "tag_destaque": "", "etiqueta_preco": "", "id_anuncio": "2",
         "url_anuncio": "https://www.zapimoveis.com.br/imovel/aluguel-apartamento-1-quarto-id-2/", "pagina": "2",
         "data_coleta": "2026-09-06T15:10:00"},
        # condomínio isento, IPTU com etiqueta colada, área em faixa
        {"bairro": "Tijuca, Rio de Janeiro", "valor_aluguel": "R$ 2.000", "valor_condominio": "isento",
         "area_util": "130 - 135 m²", "quartos": "", "bairro_busca": "Tijuca", "rua": "",
         "valor_iptu": "isento Ótimo preço", "tag_destaque": "Destaque", "etiqueta_preco": "", "id_anuncio": "3",
         "url_anuncio": "https://www.zapimoveis.com.br/imovel/aluguel-loja-salao-id-3/", "pagina": "1",
         "data_coleta": "2026-09-06T16:00:00"},
        # outlier: aluguel implausível
        {"bairro": "Leblon, Rio de Janeiro", "valor_aluguel": "R$ 15.000.000", "valor_condominio": "não informado",
         "area_util": "100 m²", "quartos": "3", "bairro_busca": "Leblon", "rua": "Rua X", "valor_iptu": "não informado",
         "tag_destaque": "", "etiqueta_preco": "", "id_anuncio": "4",
         "url_anuncio": "https://www.zapimoveis.com.br/imovel/aluguel-apartamento-3-quartos-id-4/", "pagina": "1",
         "data_coleta": "2026-09-06T17:00:00"},
        # fora do município → descartado
        {"bairro": "Jardim Guandu, Nova Iguaçu", "valor_aluguel": "R$ 7.000", "valor_condominio": "não informado",
         "area_util": "200 m²", "quartos": "", "bairro_busca": "Campo Grande", "rua": "", "valor_iptu": "não informado",
         "tag_destaque": "", "etiqueta_preco": "", "id_anuncio": "5",
         "url_anuncio": "https://www.zapimoveis.com.br/imovel/aluguel-loja-salao-id-5/", "pagina": "1",
         "data_coleta": "2026-09-06T18:00:00"},
    ])


def _tratar(df: pd.DataFrame):
    rel = pl.Relatorio()
    rel.item = lambda *a, **k: None          # silencia a saída durante o teste
    rel.titulo = lambda *a, **k: None
    descartes: list[pd.DataFrame] = []
    df = df.rename(columns={"etiqueta_preco": "etiqueta_preco_orig"})
    df = pl.normalizar_textos(df, rel)
    df = pl.filtrar_municipio(df, descartes, rel)
    df = pl.converter_numeros(df, rel)
    df = pl.derivar_atributos(df, rel)
    df = pl.remover_duplicatas(df, descartes, rel)
    df = pl.marcar_outliers(df, rel)
    df = pl.definir_apto_analise(df, rel)
    return df, descartes


def test_fluxo_completo():
    df, descartes = _tratar(_base_exemplo())

    # bairro sem a cidade, pronto para o cruzamento com o IPS
    assert set(df["bairro"]) == {"Copacabana", "Tijuca", "Leblon"}
    assert df.loc[df["bairro"] == "Copacabana", "bairro_chave"].iloc[0] == "copacabana"

    # fora do município e duplicata saíram, com o motivo registrado
    motivos = pd.concat(descartes)["motivo_descarte"].tolist()
    assert any("município" in m for m in motivos) and any("duplicata" in m for m in motivos)
    assert len(df) == 3

    copa = df[df["bairro"] == "Copacabana"].iloc[0]
    assert copa["aluguel"] == 3800.0 and copa["condominio"] == 800.0 and copa["iptu"] == 104.0
    assert copa["custo_total"] == 4600.0 and copa["preco_m2"] == round(3800 / 36, 2)
    assert copa["n_anuncios_iguais"] == 2          # o registro guarda que o imóvel aparecia duas vezes
    assert copa["tipo_imovel"] == "Apartamento" and copa["classe_imovel"] == "residencial"

    tijuca = df[df["bairro"] == "Tijuca"].iloc[0]
    assert tijuca["condominio"] == 0.0 and tijuca["condominio_informado"]      # isento é zero informado
    assert tijuca["iptu"] == 0.0 and tijuca["etiqueta_preco"] == "Ótimo preço"
    assert tijuca["area_m2"] == 132.5 and bool(tijuca["area_estimada"])
    assert pd.isna(tijuca["quartos"])                                          # comercial não tem quartos

    leblon = df[df["bairro"] == "Leblon"].iloc[0]
    assert pd.isna(leblon["condominio"]) and not leblon["condominio_informado"]  # não informado é ausente
    assert leblon["outlier"] and "aluguel acima" in leblon["motivo_outlier"]
    assert not leblon["apto_analise"]                                          # outlier fica fora da análise
    assert bool(copa["apto_analise"]) and bool(tijuca["apto_analise"])


def test_agrupamento_de_sub_bairros():
    df = _base_exemplo()
    df.loc[0, "bairro"] = "Barra Olímpica, Rio de Janeiro"
    tratado, _ = _tratar(df)
    assert "Barra da Tijuca" in set(tratado["bairro"])   # sub-bairro reagrupado no bairro oficial
    assert "Barra Olímpica" not in set(tratado["bairro"])


if __name__ == "__main__":
    testes = [obj for nome, obj in sorted(globals().items()) if nome.startswith("test_") and callable(obj)]
    for teste in testes:
        teste()
        print(f"OK  {teste.__name__}")
    print(f"\n{len(testes)} testes passaram.")
