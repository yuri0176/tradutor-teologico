# 📜 Tradutor Teológico Acadêmico

Aplicação web simples, pensada primeiro para celular, que traduz artigos,
ensaios e livros de teologia (inglês, espanhol, alemão, francês, italiano,
holandês, latim) para o português acadêmico. Tem dois motores, escolhidos na
barra lateral:

- **Gemini (grátis):** Google Gemini pela API gratuita do Google AI Studio
  (SDK `google-genai`), com limites de uso por minuto e por dia.
- **Claude (pago):** Anthropic Claude (SDK `anthropic`), cobrado por uso e
  costuma ser mais preciso em textos difíceis.

- Um system prompt especializado orienta o modelo a agir como teólogo e tradutor
  sênior. Ele cobre fidelidade hermenêutica, terminologia técnica, norma-padrão,
  grego, hebraico e latim, citações, referências e notas de rodapé (veja
  `tradutor/prompts.py`).
- Aceita texto colado ou PDF curto (até 40 páginas). PDFs digitalizados, sem
  texto selecionável, são enviados inteiros para o modelo ler as páginas como
  imagem.
- A tradução aparece enquanto é gerada. Textos longos são divididos em trechos
  por parágrafo, e cada trecho recebe o final do anterior para manter a mesma
  terminologia.
- Traz um botão grande **Copiar texto**, que funciona no celular, e a opção de
  baixar o resultado em `.md`.
- O modo **📚 Livro / PDF longo** divide o livro em capítulos, traduz um por um
  e monta o livro traduzido em **PDF**, **TXT** ou **Markdown** (veja abaixo).

## Estrutura

```
app.py                  # interface Streamlit
traduzir_livro.py       # tradução de livros pelo terminal, sem navegador
scripts/rodar_no_github.sh   # roda a tradução no GitHub Actions e guarda o progresso
.github/workflows/traduzir-livro.yml   # tradução automática diária no GitHub
requirements-cli.txt    # dependências sem o Streamlit (terminal e GitHub)
tradutor/
  prompts.py            # system prompt e montagem do pedido
  translator.py         # divisão em trechos e streaming, independente do motor
  engines/gemini.py     # motor Gemini (limites, RECITATION, servidor instável)
  engines/claude.py     # motor Claude
  common.py             # tipos compartilhados
  pdf_utils.py          # extração de texto de PDF curto
  chapters.py           # leitura de livros e divisão em capítulos
  book.py               # tradução capítulo a capítulo, com progresso salvo
  background.py         # tradução em segundo plano (continua com a página fechada)
  export.py             # reconstrução em PDF, TXT e Markdown
  book_ui.py            # interface do modo livro
  ui.py                 # CSS mobile-first e botão de copiar
  fonts/                # FreeSerif (latim, grego politônico, hebraico) para o PDF
.streamlit/config.toml  # tema e limite de upload
requirements.txt
.env.example
```

## Chaves de API

Basta uma chave. Com as duas, você troca de motor na barra lateral.

**Gemini (grátis):** entre em <https://aistudio.google.com/apikey> com a sua
conta Google, toque em **Create API key** e copie a chave (`GEMINI_API_KEY`).

**Claude (pago):** crie a chave em <https://console.anthropic.com/settings/keys>
(`ANTHROPIC_API_KEY`) e adicione créditos em *Billing*. A cobrança é à parte da
assinatura do Claude.

O plano gratuito não pede cartão de crédito, mas tem limites de pedidos por
minuto e por dia, que variam conforme o modelo. Veja os valores atuais em
<https://ai.google.dev/gemini-api/docs/rate-limits>. Segundo os termos do Google,
no plano gratuito o conteúdo enviado pode ser usado para melhorar os produtos
deles, então não envie material confidencial.

## Como rodar no computador

Requer Python 3.10 ou mais recente.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # depois edite .env e cole a(s) sua(s) chave(s)
streamlit run app.py
```

O arquivo `.env` está no `.gitignore` e não vai para o repositório.

### Abrir no celular

- **Na mesma rede Wi-Fi:** rode `streamlit run app.py --server.address 0.0.0.0`
  e abra no celular o endereço "Network URL" que aparece no terminal.
- **Em qualquer lugar:** publique no [Streamlit Community Cloud](https://streamlit.io/cloud),
  que é gratuito. Aponte para este repositório e para o arquivo `app.py`. Em
  *Settings → Secrets*, adicione:

  ```toml
  GEMINI_API_KEY = "sua-chave-do-gemini"
  ANTHROPIC_API_KEY = "sua-chave-do-claude"   # opcional
  ```

  Se o app for público, qualquer pessoa com o link vai usar a sua cota. Nesse
  caso, deixe o app privado ou não configure o secret e cole a chave na barra
  lateral a cada uso. Digitada ali, a chave fica só na sessão aberta.

## Opções

| Opção | O que faz |
|---|---|
| Idioma de origem | Detecção automática ou idioma fixo |
| Variante | Português do Brasil ou europeu |
| Referências bíblicas | `Rm 3.23` (padrão editorial brasileiro) ou `Rm 3:23` |
| Notas do tradutor | Permite `[N.T.: …]` para ambiguidades e termos sem equivalente |
| Termo original | Mostra o termo original na 1ª ocorrência: *justificação (justification)* |
| Qualidade | Profundidade de raciocínio do modelo: Alta (padrão), Máxima (mais lenta) ou Rápida |

Modelos padrão: `gemini-3.5-flash` e `claude-opus-5-5`. Para usar outros,
defina `GEMINI_MODEL` ou `ANTHROPIC_MODEL` no `.env`. No Gemini, modelos maiores
("pro") costumam ter limites gratuitos menores.

### Limites e bloqueios do Gemini

- **Limite por minuto:** quando o Gemini responde que o limite foi atingido
  (erro 429), o app mostra um aviso, espera o tempo indicado e tenta de novo
  sozinho.
- **Servidor sobrecarregado:** em erros 5xx ou quedas de conexão, o app espera
  (10 s, 20 s, 40 s…) e tenta de novo até 4 vezes. Se o texto do trecho já
  tinha começado a chegar, ele é descartado e gerado de novo, sem duplicar.
- **Cota diária:** no plano gratuito, o `gemini-3.5-flash` permite **20 pedidos por dia**
  (valor informado pelo Google para uma chave gratuita; pode mudar). Cada trecho
  traduzido é um pedido, e um capítulo costuma usar de 2 a 6. Cada modelo do Gemini
  tem a sua própria cota gratuita por dia.
  Quando a do modelo principal acaba, o app passa sozinho para o próximo da
  lista (`gemini-3.6-flash`, `gemini-3.7-flash`, `gemini-3.8-flash`, os que a sua
  chave enxergar) e registra um aviso indicando qual modelo traduziu cada trecho.
  Se todos acabarem, o app para e mostra o limite atingido; no modo livro o
  progresso fica salvo, então é só voltar depois e tocar em **Continuar**. A
  cota zera todo dia à meia-noite do horário do Pacífico (4h ou 5h da manhã no
  Brasil). Para mudar a lista, defina `GEMINI_FALLBACK_MODELS` no `.env`
  (separado por vírgulas; vazio desliga a troca). Se a sua conta tiver
  faturamento ativado no Google AI Studio, os limites são muito maiores.
- **Filtros de segurança:** ficam no nível mais permissivo que ainda bloqueia
  conteúdo grave, para não barrar discussões acadêmicas sobre guerra,
  sexualidade, violência etc.
- **RECITATION:** o Gemini interrompe a resposta quando ela reproduz
  literalmente um texto já publicado. Numa tradução, a causa típica é uma
  citação (bíblica ou de outro autor) sair idêntica a uma tradução publicada em
  português. As instruções já pedem redação própria nas citações. Se ainda
  assim acontecer, o app refaz o trecho uma vez, reforçando esse pedido. Se o
  corte se repetir, o trecho fica marcado com um aviso, e você pode refazer o
  capítulo com o Claude (veja "Refazer capítulos" abaixo).
- Outros cortes (segurança, limite de tamanho) também geram um aviso indicando
  o trecho afetado.

## Livros e PDFs longos

1. Escolha **📚 Livro / PDF longo** e envie o PDF (até 200 MB).
2. O app divide o livro em capítulos, nesta ordem de preferência:
   - pelo **sumário embutido no PDF** (marcadores), escolhendo o nível: só as
     partes, ou partes e capítulos;
   - pelos **títulos no topo das páginas** ("Chapter 3", "Capítulo IV",
     "Kapitel 2", "Part One"…);
   - em **blocos de N páginas**, se não houver nada disso.

   Partes sem conteúdo do livro (capa, folha de rosto, direitos autorais,
   sumário, lista de ilustrações, elogios, índices) começam **desmarcadas**,
   porque cada parte marcada gasta um pedido da cota diária; marque de volta se
   quiser. Você pode trocar o método, renomear os capítulos, desmarcar o que não
   quiser traduzir ou escolher uma faixa em **"Traduzir da parte __ até a parte
   __"**. Cabeçalhos e rodapés corridos e números de página são
   removidos antes do envio.
3. O app mostra quantos **pedidos à API** e quantos **tokens** a tradução vai
   usar. No Gemini, compare com os limites diários do plano gratuito, porque um
   livro inteiro pode levar mais de um dia. No Claude, aparece o custo estimado
   em dólares.
4. Os capítulos vão para a API **vários ao mesmo tempo** (o controle **"Capítulos ao
   mesmo tempo"**, em Opções avançadas, vai de 1 a 5; o padrão é 3). Dentro de
   cada capítulo, os trechos seguem em ordem: capítulos longos são divididos por
   parágrafo, e cada trecho recebe o final do anterior para manter a mesma
   terminologia. Em paralelo o texto não aparece ao vivo; com 1 capítulo por vez,
   aparece. Se o Gemini pedir para esperar (limite por minuto), o app espera sozinho.
   A leitura do PDF também é rápida: um livro de 1.900 páginas leva cerca de 10 s.
5. **Cada trecho traduzido é salvo em disco** (pasta `.traducoes/`) assim que
   termina. Dá para tocar em **Pausar**, fechar a página, perder a conexão ou
   esgotar a cota diária: ao enviar o mesmo PDF com as mesmas opções, o botão
   vira **Continuar tradução** e retoma de onde parou, sem refazer o que já foi
   feito. Também dá para marcar mais capítulos depois e continuar o mesmo
   trabalho.
6. **Refazer capítulos:** em "Refazer capítulos", escolha capítulos já
   traduzidos, apague a tradução deles, troque o motor na barra lateral e toque
   em **Continuar tradução**. Um mesmo livro pode ter capítulos feitos pelo
   Gemini e outros pelo Claude. Isso é útil para os trechos mais difíceis ou
   para os que o Gemini cortou.
7. Ao final, ou a qualquer momento com o que já estiver pronto, baixe o livro:
   - **PDF**: formato A5, com folha de rosto, marcadores por capítulo, notas
     no fim de cada capítulo e fonte com grego e hebraico;
   - **TXT**: texto simples;
   - **Markdown**: preserva itálicos, títulos e notas.

Livros digitalizados, sem texto selecionável, também funcionam: as páginas são
enviadas como imagem, 12 por vez.

### Segundo plano (com a página fechada)

O botão **🌙 Traduzir livro em segundo plano** inicia a tradução numa thread do
servidor: você pode fechar a página ou bloquear o celular e o trabalho continua.
Ao voltar, envie o mesmo PDF e o painel mostra o andamento (ou o livro pronto).
Se a cota diária do Gemini acabar em todos os modelos, o app **espera a cota
zerar** (meia-noite do Pacífico, cerca de 4h da manhã no Brasil) e continua
sozinho. Erros passageiros de servidor também são esperados (5 min) e repetidos.
O botão **⏹️ Parar** interrompe, e o progresso fica salvo.

Limites do Streamlit gratuito: o app **adormece** depois de muitas horas sem nenhuma
visita (cerca de 12 h) e o disco **é apagado** se ele reiniciar; a chave da API só
existe na memória do servidor. Para rodadas de algumas horas isso basta. Para
vários dias, abra o app de vez em quando (manda o app ficar acordado) e baixe o
**arquivo de progresso**.

### Vários dias, sem celular nem computador ligados (GitHub Actions)

Para um livro inteiro com o plano gratuito, use o trabalhador do GitHub
(`.github/workflows/traduzir-livro.yml`). Ele roda no servidor do GitHub todo dia,
logo depois que a cota do Gemini zera (04h25 e 05h25 em Brasília), traduz até a
cota acabar, **guarda o progresso no repositório** (pasta `saida/`) e continua no dia
seguinte. Quando termina, cria `saida/CONCLUIDO.txt` e as execuções seguintes não
fazem mais nada.

**Precisa ser um repositório PRIVADO**, porque o PDF do livro e a tradução ficam nele.
Nunca torne público um repositório que tenha o livro.

Configuração (uma vez, dá para fazer pelo navegador do celular; se faltarem botões,
use "Site para computador" no menu do navegador):

1. Deixe o PDF na raiz do repositório e confira o nome em `LIVRO` no arquivo
   `.github/workflows/traduzir-livro.yml` (o padrão já é o nome do PDF em inglês).
2. Em **Settings → Secrets and variables → Actions → New repository secret**, crie
   `GEMINI_API_KEY` com a sua chave. (`ANTHROPIC_API_KEY` é opcional.) Em **Variables**,
   se quiser, crie `TITULO_LIVRO` com o título da capa do PDF.
3. Em **Actions → Traduzir livro → Run workflow**, coloque `10-12` em "Partes a
   traduzir" para um **teste** (Prefácio e Capítulo 1). Depois abra a execução: o
   **resumo** mostra partes prontas, palavras, minutos esperando o Google e modelos usados.
4. Se o teste for bom, rode de novo com "Partes a traduzir" **vazio** (o livro todo,
   sem capa nem sumário). A partir daí ele roda sozinho todo dia até terminar.
5. Os arquivos ficam em `saida/` (`livro.pdf`, `livro.txt`, `livro.md`): abra o arquivo
   no GitHub e use **Download** / **Raw**.

Detalhes:
- O plano gratuito de repositórios privados dá 2.000 minutos de Actions por mês; cada
  execução pára sozinha quando a cota do dia acaba.
- Mudar idioma, qualidade, referências ou a divisão em capítulos cria um trabalho novo
  (não aproveita o progresso antigo). Mudar só as partes escolhidas não.
- Para parar: **Actions → Traduzir livro → ⋯ → Disable workflow**.
- No terminal, `python traduzir_livro.py livro.pdf --cota-acabou sair --saidas-so-no-fim`
  faz o mesmo que uma execução do GitHub; sem essas opções, ele espera a cota zerar e
  continua sozinho.

### Arquivo de progresso

A pasta `.traducoes/` guarda o progresso, mas no Streamlit Community Cloud ela
some quando o app hiberna ou reinicia. Para não perder o trabalho de um dia
para o outro, use **💾 Arquivo de progresso**:

1. Ao parar, toque em **Baixar progresso (.json)**.
2. Da próxima vez, envie o mesmo PDF, escolha **a mesma divisão de capítulos** e
   reenvie esse arquivo em **Retomar de um arquivo de progresso**.
3. A tradução continua de onde parou, e no fim sai um PDF único.

O arquivo guarda só o texto traduzido: nenhuma chave de API vai nele.

## Traduzir um livro pelo terminal

No computador, `traduzir_livro.py` faz o mesmo trabalho sem navegador. Ele roda
sozinho por horas, salva o progresso a cada trecho e regrava o livro a cada
capítulo terminado, então pode ser interrompido a qualquer momento.

```bash
# ver os capítulos encontrados
python traduzir_livro.py livro.pdf --listar

# traduzir tudo
python traduzir_livro.py livro.pdf --titulo "Ética Cristã"

# só alguns capítulos, do inglês, com o Claude
python traduzir_livro.py livro.pdf --capitulos 1-8,12 --idioma inglês --motor claude
```

Rode o mesmo comando de novo para continuar de onde parou. Opções principais:

| Opção | Para quê |
|---|---|
| `--listar` | mostra os capítulos e sai, sem traduzir |
| `--capitulos 1-8,12` | escolhe as partes, pelos números do `--listar` |
| `--dividir auto\|sumario\|titulos\|paginas` | como separar os capítulos |
| `--nivel 2` / `--paginas 20` | detalhe da divisão por sumário ou por blocos |
| `--motor gemini\|claude` | qual API usar |
| `--qualidade rapida\|alta\|maxima` | profundidade de raciocínio do modelo |
| `--paralelo 3` | capítulos traduzidos ao mesmo tempo (padrão 3; 1 = um por vez) |
| `--cota-acabou esperar\|sair` | cota diária esgotada: espera zerar e continua (padrão) ou sai |
| `--tudo` | inclui capa, sumário e páginas quase vazias (por padrão são puladas) |
| `--idioma inglês` | idioma de origem (padrão: detectar) |
| `--saida caminho/nome` | nome-base dos arquivos gerados |
| `--notas-tradutor`, `--termo-original` | as mesmas opções do app |

Ao final ficam três arquivos lado a lado: `.pdf`, `.txt` e `.md`.
