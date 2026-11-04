#!/bin/sh
# 让本仓库使用 scripts/hooks/ 下的钩子（每个 clone 跑一次）。
set -e

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
git config core.hooksPath scripts/hooks
echo "core.hooksPath -> scripts/hooks"
