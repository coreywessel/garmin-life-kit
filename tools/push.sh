#!/usr/bin/env bash
# Push this run's data commit. Another job may have pushed in the meantime, so rebase onto it
# and retry (3 tries with backoff). A real conflict fails the run loudly instead of retrying.
#
# The one conflict that is fine: data/*.enc (the encrypted Garmin token cache). Two jobs side by
# side can each save a fresh token. Either token works, so keep main's copy (during a rebase,
# --ours is main), drop this commit if nothing else is left in it, and carry on.
set -u

rebasing() { [ -d "$(git rev-parse --git-path rebase-merge)" ] || [ -d "$(git rev-parse --git-path rebase-apply)" ]; }

# Returns 0 once the rebase is finished. Returns 1, still mid-rebase, on any other conflict.
keep_main_enc() {
  while rebasing; do
    conflicts=$(git diff --name-only --diff-filter=U)
    [ -n "$conflicts" ] || return 1
    if printf '%s\n' "$conflicts" | grep -qv '^data/.*\.enc$'; then return 1; fi
    echo "::warning::kept main's copy of: $(echo $conflicts)"
    printf '%s\n' "$conflicts" | while read -r f; do
      git checkout -q --ours -- "$f" && git add -- "$f" || exit 1
    done || return 1
    # Nothing left of this commit once main's token is kept: drop it instead of continuing.
    if git diff --cached --quiet; then git rebase --skip >/dev/null 2>&1 || true
    else GIT_EDITOR=true git rebase --continue >/dev/null 2>&1 || true; fi
  done
}

for i in 1 2 3; do
  if git pull -q --rebase && git push -q; then exit 0; fi
  if rebasing; then
    if keep_main_enc; then
      if git push -q; then exit 0; fi
    else
      echo "::error::data commit conflicts with a newer commit on main ($(git diff --name-only --diff-filter=U | tr '\n' ' ')) - not pushed"
      git rebase --abort
      exit 1
    fi
  fi
  sleep $((i * 5))
done
echo "::error::could not push the data commit after 3 tries"
exit 1
