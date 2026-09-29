#!/usr/bin/env bash
# Roda a tradução do livro no GitHub Actions e guarda o progresso no próprio repositório.
#
#   rodar_no_github.sh            traduz, guardando o progresso a cada ~10 minutos
#   rodar_no_github.sh --guardar  só guarda o que houver em saida/ (usado ao fim, mesmo após erro)
#
# Variáveis: LIVRO, TITULO, IDIOMA, MOTOR, QUALIDADE, PARALELO, CAPITULOS (opcional),
# BRANCH (padrão: a atual), COMMIT_A_CADA (segundos, padrão 600) e TRADUZIR_CMD (para testes).
set -uo pipefail

BRANCH="${BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
COMMIT_A_CADA="${COMMIT_A_CADA:-600}"
export TRADUTOR_JOBS_DIR="saida/progresso"
mkdir -p saida/progresso

guardar() {
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  git add -A saida
  if git diff --cached --quiet; then
    return 0
  fi
  git commit -q -m "$1"
  local i
  for i in 1 2 3 4; do
    if git pull -q --rebase --autostash origin "$BRANCH" && git push -q origin "HEAD:$BRANCH"; then
      return 0
    fi
    sleep $((i * 5))
  done
  echo "aviso: não consegui enviar o progresso ao repositório" >&2
  return 1
}

if [ "${1:-}" = "--guardar" ]; then
  guardar "Guardar progresso da tradução"
  exit 0
fi

if [ ! -f "${LIVRO:-}" ]; then
  echo "Livro não encontrado: '${LIVRO:-}'. Coloque o PDF no repositório ou informe o caminho certo." >&2
  exit 2
fi

args=("$LIVRO" --titulo "${TITULO:-$(basename "$LIVRO" .pdf)}" --saida saida/livro
      --idioma "${IDIOMA:-inglês}" --motor "${MOTOR:-gemini}" --qualidade "${QUALIDADE:-alta}"
      --paralelo "${PARALELO:-3}" --cota-acabou sair --saidas-so-no-fim)
if [ -n "${CAPITULOS:-}" ]; then
  args+=(--capitulos "$CAPITULOS")
fi

# shellcheck disable=SC2086
${TRADUZIR_CMD:-python traduzir_livro.py} "${args[@]}" &
pid=$!

# De tempos em tempos guarda o progresso: se a execução for cortada, só se perde o trecho em andamento.
while kill -0 "$pid" 2>/dev/null; do
  for ((t = 0; t < COMMIT_A_CADA; t++)); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    guardar "Progresso da tradução"
  fi
done
wait "$pid"
code=$?

guardar "Tradução: progresso e arquivos gerados"
exit "$code"
