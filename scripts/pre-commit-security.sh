#!/usr/bin/env bash
# Blocks commits containing likely secrets.
# Patterns: OpenAI/Anthropic sk- keys, AWS keys, bearer tokens, .envrc additions.

set -euo pipefail

RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

FAIL=0
STAGED_FILES=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null || true)

if [ -z "$STAGED_FILES" ]; then
  exit 0
fi

# Pattern: Anthropic/OpenAI secret keys
SECRET_KEY_PATTERN='sk-[a-zA-Z0-9_-]{20,}'
# Pattern: AWS access key
AWS_KEY_PATTERN='AKIA[0-9A-Z]{16}'
# Pattern: Bearer token literals
BEARER_PATTERN='Bearer [a-zA-Z0-9_\-\.]{20,}'
# Pattern: Generic high-entropy assignments
GENERIC_SECRET_PATTERN='(password|secret|token|key)\s*=\s*["\x27][a-zA-Z0-9_\-\.]{16,}["\x27]'

check_file() {
  local file="$1"

  # Skip binary files
  if ! git diff --cached -- "$file" | grep -q '^+'; then
    return
  fi

  # Skip template/example files — placeholders intentionally look like secrets
  if [[ "$file" == *.example || "$file" == *.sample || "$file" == *.template ]]; then
    return
  fi

  # Check for .envrc being staged
  if [[ "$file" == ".envrc" ]]; then
    echo -e "${RED}BLOCKED${NC}: Attempt to commit .envrc (contains secrets)"
    echo "  File: $file"
    FAIL=1
    return
  fi

  # Get only added lines from the diff
  local added_lines
  added_lines=$(git diff --cached -- "$file" | grep '^+' | grep -v '^+++' || true)

  if echo "$added_lines" | grep -qP "$SECRET_KEY_PATTERN" 2>/dev/null || \
     echo "$added_lines" | grep -qE "$SECRET_KEY_PATTERN"; then
    echo -e "${RED}BLOCKED${NC}: Possible API key (sk-...) in staged changes"
    echo "  File: $file"
    FAIL=1
  fi

  if echo "$added_lines" | grep -qE "$AWS_KEY_PATTERN"; then
    echo -e "${RED}BLOCKED${NC}: Possible AWS access key in staged changes"
    echo "  File: $file"
    FAIL=1
  fi

  if echo "$added_lines" | grep -qiE "$GENERIC_SECRET_PATTERN"; then
    echo -e "${YELLOW}WARNING${NC}: Possible hardcoded secret/password/token assignment"
    echo "  File: $file"
    echo "  (If this is a false positive, use: git commit --no-verify)"
    FAIL=1
  fi
}

for file in $STAGED_FILES; do
  check_file "$file"
done

if [ "$FAIL" -ne 0 ]; then
  echo ""
  echo -e "${RED}Commit blocked: potential secrets detected.${NC}"
  echo "Review the above files and remove secrets before committing."
  echo "Use .envrc for local secrets (already in .gitignore)."
  exit 1
fi

exit 0
