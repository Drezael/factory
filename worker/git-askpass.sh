#!/bin/sh
# Used only for authenticated github.com clone operations; never logs the token.
case "${1:-}" in
  *Username*) printf 'x-access-token\n' ;;
  *Password*) printf '%s\n' "${FACTORY_GITHUB_TOKEN:-}" ;;
  *) printf '\n' ;;
esac
