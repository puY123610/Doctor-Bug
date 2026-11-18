#!/usr/bin/env node
/*
 * Pack the Python platform into npm/platform before `npm pack`.
 *
 * The npm tarball must be self-contained: a user installs it on a machine that
 * has Node but never had this repository checked out.  We copy only runtime
 * assets (never .env.local, model_config.json, runs, caches or a venv).
 */
"use strict";

const fs = require("fs");
const path = require("path");

const packageRoot = __dirname;
const sourceRoot = path.resolve(packageRoot, "..");
const targetRoot = path.join(packageRoot, "platform");

const ENTRIES = [
  "core",
  "acceptor",
  "kb",
  "market",
  "agentscope_ext",
  "tools",
  "requirements.txt",
  "pyproject.toml",
  "model_config.example.json",
];

const EXCLUDED_NAMES = new Set([
  "__pycache__",
  ".pytest_cache",
  ".mypy_cache",
  ".ruff_cache",
  ".DS_Store",
  ".git",
  ".hg",
  ".svn",
  ".venv",
  "venv",
  "node_modules",
  "runs",
  ".env.local",
  "wiring_demo.py",
  "model_config.json",
  ".env",
]);

const EXCLUDED_SUFFIXES = [".pyc", ".pyo"];

// Source-relative paths that must never ship: the built-in demo vaults
// and the hand-written default dispatch tables. Installed products
// judge and dispatch only from each workspace's own frozen profile.
const EXCLUDED_PATHS = new Set([
  "kb/dispatch_table.json",
  "kb/dispatch_table_repo2.json",
  "agentscope_ext/fake_model.py",
]);

function isExcludedPath(source) {
  const rel = path.relative(sourceRoot, source).split(path.sep).join("/");
  return EXCLUDED_PATHS.has(rel);
}

function copyRecursive(source, target) {
  if (isExcludedPath(source)) {
    return;
  }
  const stat = fs.lstatSync(source);
  if (stat.isSymbolicLink()) {
    const real = fs.realpathSync(source);
    const realStat = fs.statSync(real);
    if (realStat.isDirectory()) {
      copyRecursive(real, target);
    } else {
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.copyFileSync(real, target);
    }
    return;
  }
  if (stat.isDirectory()) {
    const name = path.basename(source);
    if (EXCLUDED_NAMES.has(name)) {
      return;
    }
    fs.mkdirSync(target, { recursive: true });
    for (const entry of fs.readdirSync(source)) {
      copyRecursive(path.join(source, entry), path.join(target, entry));
    }
    return;
  }
  if (!stat.isFile()) {
    return;
  }
  const name = path.basename(source);
  if (EXCLUDED_NAMES.has(name)) {
    return;
  }
  if (EXCLUDED_SUFFIXES.some((suffix) => name.endsWith(suffix))) {
    return;
  }
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.copyFileSync(source, target);
}

function copyEntry(entry) {
  const source = path.join(sourceRoot, entry);
  if (!fs.existsSync(source)) {
    throw new Error(`missing platform asset: ${entry}`);
  }
  copyRecursive(source, path.join(targetRoot, entry));
}

function main() {
  if (process.argv.includes("--dry-run")) {
    console.log(
      JSON.stringify(
        {
          sourceRoot,
          targetRoot,
          entries: ENTRIES,
          excludes: [...EXCLUDED_NAMES],
          excludedPaths: [...EXCLUDED_PATHS],
        },
        null,
        2,
      ),
    );
    return;
  }

  fs.rmSync(targetRoot, { recursive: true, force: true });
  fs.mkdirSync(targetRoot, { recursive: true });
  for (const entry of ENTRIES) {
    copyEntry(entry);
  }

  const readme = path.join(sourceRoot, "README.md");
  if (fs.existsSync(readme)) {
    fs.copyFileSync(readme, path.join(packageRoot, "README.md"));
  }

  const marker = path.join(targetRoot, "INSTALLATION.json");
  fs.writeFileSync(
    marker,
    JSON.stringify(
      {
        packaged_by: "drbug npm prepack",
        package_version: require("./package.json").version,
      },
      null,
      2,
    ) + "\n",
  );
}

main();
