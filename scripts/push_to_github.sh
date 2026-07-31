#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_URL="https://github.com/ShojiJohn1008/Datapipeline_Multi.git"
REMOTE="origin"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

if [[ ! -d "$REPOSITORY_ROOT/.git" ]]; then
  echo "エラー: $REPOSITORY_ROOT はGitリポジトリではありません。" >&2
  echo "このスクリプトは、開発Macにあるclone済みリポジトリ内で実行してください。" >&2
  exit 1
fi

cd "$REPOSITORY_ROOT"

BRANCH="${1:-$(git branch --show-current)}"
if [[ -z "$BRANCH" ]]; then
  echo "エラー: detached HEADです。pushするブランチ名を引数で指定してください。" >&2
  echo "例: $0 codex" >&2
  exit 1
fi

if git remote get-url "$REMOTE" >/dev/null 2>&1; then
  git remote set-url "$REMOTE" "$REPOSITORY_URL"
else
  git remote add "$REMOTE" "$REPOSITORY_URL"
fi

echo "Push先: $REPOSITORY_URL"
echo "ブランチ: $BRANCH"
git status --short --branch
git push --set-upstream "$REMOTE" "$BRANCH"
