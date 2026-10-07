# Análise do mercado de aluguel do Rio de Janeiro e o IPS

Projeto de Graduação em Sistemas de Informação (UNIRIO). O objetivo é medir a relação
entre o preço do aluguel e o Índice de Progresso Social (IPS) nas regiões do município do
Rio de Janeiro, e identificar onde os dois se descolam.

Tudo é feito por código, em três camadas encadeadas — coleta, tratamento e análise —, de
modo que qualquer número do TCC possa ser reproduzido a partir dos dados brutos.

---

## Estrutura da pasta

Cada camada tem sua própria pasta, numerada na ordem em que é executada, com o **script** e
as **saídas dele** juntos. Para entender o que uma camada faz, basta abrir a pasta dela.

```
zap-scraper/
│
├── README.md                     este arquivo
├── requirements.txt              bibliotecas necessárias
│
├── 0_entrada/                    INSUMOS EXTERNOS — nada aqui é gerado por código
│   ├── bairros_rj.csv                quadro amostral: 162 bairros, zona e slug de busca
│   ├── depara_bairros_ra.xlsx        de-para de bairro para região administrativa
│   └── ips_rio.xlsx                  planilha do IPS 2024 (Instituto Pereira Passos)
│
├── 1_bronze/                     COLETA — extrai os anúncios do portal, sem tratar nada
│   ├── zap_scraper.py                o robô
│   ├── zap_imoveis_raw_rj.csv        base bruta (17.945 anúncios)
│   ├── zap_scraper.log               registro de todas as URLs visitadas — evidência da coleta
│   └── progresso_coleta.json         bairros já concluídos, permite retomar de onde parou
│
├── 2_prata/                      TRATAMENTO — tipa, deduplica e cruza com o IPS
│   ├── prata_limpeza.py
│   ├── zap_imoveis_prata.csv         base tratada (16.457 imóveis únicos, 32 colunas)
│   ├── zap_imoveis_prata_analise.csv só as linhas aptas à análise (16.068)
│   ├── prata_descartes.csv           o que saiu da base, com o motivo de cada descarte
│   ├── bairros_para_depara.csv       lista de bairros, para conferir a cobertura do de-para
│   └── relatorio_prata.txt           contagem de cada etapa do tratamento
│
├── 3_ouro/                       ANÁLISE — estatísticas, tabelas e figuras
│   ├── ouro_analise.py
│   ├── ouro_regioes.csv              uma linha por região: medianas, IPS, desvio, posições
│   ├── ouro_bairros.csv              uma linha por bairro (descritivo)
│   ├── ouro_zonas.csv                uma linha por zona da cidade
│   ├── ouro_correlacoes.csv          todas as correlações calculadas
│   ├── relatorio_ouro.txt            a análise completa em texto
│   └── figuras/                      as cinco figuras do Capítulo 6, em PNG 200 dpi
│
├── tests/                        testes automatizados (rodam sem internet)
└── tcc/                          as versões do documento e a documentação auxiliar
```

O fluxo é sempre o mesmo: `0_entrada` + `1_bronze` → `2_prata` → `3_ouro`. Cada camada só
lê da anterior e nunca a altera, de modo que qualquer etapa pode ser refeita sem repetir a
coleta.

---

## Instalação

Uma única vez, a partir da pasta `Web Scrapping`:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r zap-scraper\requirements.txt
```

Depois disso, use sempre `..\.venv\Scripts\python.exe` no lugar de `python` — é o
interpretador do ambiente virtual, onde as bibliotecas foram instaladas.

---

## Como rodar

Os comandos abaixo pressupõem que você está dentro da pasta da camada.

**Camada Bronze** (coleta — exige Chrome instalado e leva horas):

```powershell
cd zap-scraper\1_bronze
..\..\.venv\Scripts\python.exe zap_scraper.py --teste      # 3 bairros, para conferir
..\..\.venv\Scripts\python.exe zap_scraper.py --todos      # coleta completa
..\..\.venv\Scripts\python.exe zap_scraper.py --continuar  # retoma uma coleta interrompida
```

**Camada Prata** (tratamento — segundos):

```powershell
cd ..\2_prata
..\..\.venv\Scripts\python.exe prata_limpeza.py
```

**Camada Ouro** (análise e figuras — segundos):

```powershell
cd ..\3_ouro
..\..\.venv\Scripts\python.exe ouro_analise.py
```

Os scripts também funcionam se você os chamar a partir da raiz do projeto
(`python 2_prata\prata_limpeza.py`): cada um localiza sozinho as pastas de que precisa.

---

## Como gerar uma análise atualizada

Este é o roteiro para refazer o trabalho com dados novos — uma coleta mais recente do
portal, uma edição mais nova do IPS, ou as duas coisas.

### 1. Guarde a rodada anterior

Antes de qualquer coisa, renomeie os arquivos que serão sobrescritos, para poder comparar
depois e não perder a base que sustenta o texto atual do TCC:

```powershell
cd zap-scraper\1_bronze
ren zap_imoveis_raw_rj.csv zap_imoveis_raw_rj_2026-09.csv
ren progresso_coleta.json progresso_coleta_2026-09.json
```

Renomear o `progresso_coleta.json` é **obrigatório** para uma coleta nova: se ele
permanecer, o robô considerará todos os bairros já concluídos e não fará nada.

### 2. Nova coleta do ZAP Imóveis

O portal muda o HTML de tempos em tempos, e é isso que quebra um scraper. Antes da coleta
completa, rode o teste:

```powershell
..\..\.venv\Scripts\python.exe zap_scraper.py --teste
```

- **Se vierem cerca de 90 anúncios**, está tudo certo — pode rodar `--todos`.
- **Se vierem zero**, os seletores mudaram. Abra o `zap_scraper.py`, vá até o dicionário
  `SELETORES` no início do arquivo, inspecione o HTML de uma página de busca no navegador
  (botão direito → Inspecionar) e atualize os atributos `data-cy`. Só isso costuma bastar;
  eles foram deixados no topo do arquivo exatamente para esse caso.
- Vale conferir também se o `0_entrada/bairros_rj.csv` continua válido: o portal cria e
  remove páginas de bairro ao longo do tempo. Bairros que sumirem aparecem no log como
  abortados.

A coleta completa leva cerca de dois dias, por causa das pausas entre requisições. Se
parar no meio (queda de energia, Chrome travado), retome com `--continuar`.

### 3. IPS mais recente

Baixe a planilha nova do Data.Rio e salve-a em `0_entrada/`, substituindo a atual, com o
nome `ips_rio.xlsx`. Três observações:

- O leitor localiza a aba pelo nome que **contenha** "Dimensões e Componentes", então uma
  aba chamada "Dimensões e Componentes 2026" é reconhecida sem nenhum ajuste.
- Ele também remove sozinho o numeral romano do nome da região ("XIII MÉIER") e descarta a
  linha do município como um todo.
- Se a Prefeitura reorganizar as colunas da planilha, o ajuste fica em `ler_ips_datario`,
  no `prata_limpeza.py`.

O `depara_bairros_ra.xlsx` só precisa ser trocado se o município criar, extinguir ou
redesenhar regiões administrativas — o que é raro.

### 4. Reprocessar

```powershell
cd ..\2_prata
..\..\.venv\Scripts\python.exe prata_limpeza.py
cd ..\3_ouro
..\..\.venv\Scripts\python.exe ouro_analise.py
```

Isso regenera tudo: as bases tratadas, os dois relatórios, as quatro tabelas e as cinco
figuras. Nenhum passo manual é necessário entre as duas camadas.

### 5. Conferir antes de usar

Abra os dois relatórios e verifique estes pontos, que são onde um dado novo costuma
revelar problema:

| Onde | O que conferir | O que significa se mudar |
|---|---|---|
| `relatorio_prata.txt` | cobertura do de-para (era 100%) | caiu: o portal passou a listar um bairro que não está no de-para |
| `relatorio_prata.txt` | cobertura do IPS (era 99,97%) | caiu: apareceu região sem nota na planilha nova |
| `relatorio_prata.txt` | duplicatas removidas | salto grande: possível repetição de coleta |
| `relatorio_ouro.txt` | regiões que passam o corte (eram 25) | menos regiões: coleta menor ou mais concentrada |
| `relatorio_ouro.txt` | correlação e R² | compare com a rodada anterior antes de reescrever o texto |

### 6. Atualizar o TCC

Este passo **não é automático**, e vale ter isso claro: os números dos Capítulos 5 e 6 do
documento foram escritos a partir dos relatórios, mas estão fixos no texto. Uma rodada
nova exige revisá-los — as tabelas 7 a 10 e as cinco figuras vêm direto dos arquivos de
`3_ouro/`, mas os números citados no meio dos parágrafos precisam ser conferidos um a um.

---

## Onde nasce cada elemento do TCC

| Elemento do documento | Origem |
|---|---|
| Tabela 1 – dicionário da base bruta | `1_bronze/zap_imoveis_raw_rj.csv` |
| Figura 1 – fluxo da coleta | diagrama, feito à parte |
| Capítulo 5 (números do tratamento) | `2_prata/relatorio_prata.txt` |
| Figura 2 – funil do tratamento | `2_prata/relatorio_prata.txt` |
| Tabela 7 – perfil por zona | `3_ouro/ouro_zonas.csv` |
| Tabela 8 – correlações por dimensão | `3_ouro/ouro_correlacoes.csv` |
| Tabela 9 – desvios por região | `3_ouro/ouro_regioes.csv` |
| Tabela 10 – sensibilidade ao corte | `3_ouro/ouro_correlacoes.csv` |
| Figuras 3 a 7 | `3_ouro/figuras/` |

---

## O que é fácil de ajustar

Cada script concentra suas decisões em uma seção de configuração no início do arquivo,
justamente para que mudanças não exijam mexer na lógica:

- **`1_bronze/zap_scraper.py`** — os seletores do HTML, o limite de páginas por bairro
  (30), os tempos de espera entre requisições (4 a 9 segundos) e a ordenação da busca
  (`MOST_RECENT`, que evita o viés dos anúncios patrocinados).
- **`2_prata/prata_limpeza.py`** — a chave que define duas linhas como o mesmo imóvel, os
  limites de plausibilidade (aluguel entre R$ 300 e R$ 1.000.000, área entre 10 e
  10.000 m², e assim por diante) e a tabela de sub-bairros reagrupados.
- **`3_ouro/ouro_analise.py`** — o corte mínimo de anúncios por região (30), o recorte de
  classe (residencial), os cortes testados na análise de robustez e a paleta das figuras.

---

## Testes

Rodam em segundos, sem internet, sobre uma página real salva em `tests/fixtures/`:

```powershell
cd zap-scraper
..\.venv\Scripts\python.exe tests\test_parser.py    # 12 testes da extração
..\.venv\Scripts\python.exe tests\test_prata.py     #  7 testes do tratamento
```

Vale rodá-los depois de qualquer alteração nos seletores ou nas regras de conversão: eles
cobrem justamente os casos-limite reais encontrados na coleta (condomínio isento, área em
faixa, etiqueta promocional colada ao valor, anúncio de outro município).
