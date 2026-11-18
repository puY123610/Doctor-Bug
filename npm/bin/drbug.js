#!/usr/bin/env node
/*
 * drbug npm launcher.
 *
 * Bundled packages (built by build_bundled.sh) carry a ready-made Python
 * runtime + preinstalled libraries under platform/runtime: when the
 * bundle matches this platform and the pinned requirements digest, the
 * launcher uses it directly and the first run needs no setup at all.
 *
 * Plain packages bootstrap a private Python environment under ~/.drbug
 * on first run:
 *   1) prefer an existing Python >= 3.11 on PATH;
 *   2) otherwise download a pinned python-build-standalone build (sha256
 *      verified) into ~/.drbug/runtime;
 *   3) create ~/.drbug/venv and install requirements.txt once.
 *
 * Then it runs `python -m core.cli` with the user's current directory as cwd,
 * forwarding argv and the exact exit code.  The platform source stays in the
 * npm package directory; cwd is never changed to the installation tree.
 */
"use strict";

const crypto = require("crypto");
const fs = require("fs");
const https = require("https");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const realFile = fs.realpathSync(__filename);
const packageRoot = path.resolve(path.dirname(realFile), "..");
const platformRoot = path.join(packageRoot, "platform");
const drbugHome = process.env.DRBUG_HOME
  ? path.resolve(process.env.DRBUG_HOME)
  : path.join(os.homedir(), ".drbug");
const venvDir = path.join(drbugHome, "venv");
const venvMarker = path.join(venvDir, ".drbug-bootstrap.json");

const PINNED_PYTHON = "3.12.16";
const PINNED_RELEASE = "20261110";
const RELEASE_BASE = `https://github.com/astral-sh/python-build-standalone/releases/download/${PINNED_RELEASE}/`;

const RUNTIME_ASSETS = {
  "darwin-arm64": {
    filename: `cpython-${PINNED_PYTHON}+${PINNED_RELEASE}-aarch64-apple-darwin-install_only.tar.gz`,
    sha256:
      "e956e60bc9465d577ef8062059aa05cd3a2665b4b8678a2ed24e432368330b8f",
  },
  "darwin-x64": {
    filename: `cpython-${PINNED_PYTHON}+${PINNED_RELEASE}-x86_64-apple-darwin-install_only.tar.gz`,
    sha256:
      "83f2922437e6d546d0fcdb8c11f150a8e52787e8d9ea8651b1974e20908eb1bc",
  },
  "linux-x64": {
    filename: `cpython-${PINNED_PYTHON}+${PINNED_RELEASE}-x86_64-unknown-linux-gnu-install_only.tar.gz`,
    sha256:
      "c2f87d4b4e8db3abddb0f116c6ccfc503d0cbdbb9ee8faacddad1b679ec12e99",
  },
  "linux-arm64": {
    filename: `cpython-${PINNED_PYTHON}+${PINNED_RELEASE}-aarch64-unknown-linux-gnu-install_only.tar.gz`,
    sha256:
      "4792f81750282597fb2a3389fe18b945ed2ba6aad5e45ad406d122c58cb33081",
  },
  "win32-x64": {
    filename: `cpython-${PINNED_PYTHON}+${PINNED_RELEASE}-x86_64-pc-windows-msvc-install_only.tar.gz`,
    sha256:
      "e23fe5b16926de1d81661b1a97352e89fb167549c55a6825796ccd0f32ec7f1b",
  },
  "win32-arm64": {
    filename: `cpython-${PINNED_PYTHON}+${PINNED_RELEASE}-aarch64-pc-windows-msvc-install_only.tar.gz`,
    sha256:
      "7122bf62a0cf601527ba62e19ca715d64db86b269765da23937e9ac7f968f39c",
  },
};

function fail(message) {
  console.error(`drbug: ${message}`);
  process.exit(1);
}

function note(message) {
  console.error(`drbug: ${message}`);
}

function exe(name) {
  return process.platform === "win32" ? `${name}.exe` : name;
}

function childEnv() {
  const additions = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    path.join(os.homedir(), ".local", "bin"),
  ];
  const current = process.env.PATH || "";
  return {
    ...process.env,
    PATH: [...additions, current].filter(Boolean).join(path.delimiter),
    PIP_DISABLE_PIP_VERSION_CHECK: "1",
    PIP_NO_INPUT: "1",
    PYTHONNOUSERSITE: "1",
  };
}

function runCapture(command, args) {
  return spawnSync(command, args, {
    encoding: "utf8",
    env: childEnv(),
    timeout: 30000,
  });
}

function pythonVersion(command) {
  const result = runCapture(command, [
    "-c",
    "import sys; print('%d.%d.%d' % sys.version_info[:3])",
  ]);
  if (result.error || result.status !== 0) {
    return null;
  }
  const match = /^(\d+)\.(\d+)\.(\d+)/.exec((result.stdout || "").trim());
  if (!match) {
    return null;
  }
  const version = [Number(match[1]), Number(match[2]), Number(match[3])];
  if (version[0] < 3 || (version[0] === 3 && version[1] < 11)) {
    return null;
  }
  return { command, version: version.join(".") };
}

function findPython() {
  const override = (process.env.DRBUG_PYTHON || "").trim();
  const candidates = [];
  if (override) {
    candidates.push(override);
  }
  candidates.push("python3.12", "python3.13", "python3.11", "python3");
  for (const candidate of candidates) {
    const found = pythonVersion(candidate);
    if (found) {
      return found;
    }
  }
  return null;
}

function runtimeAsset() {
  return RUNTIME_ASSETS[`${process.platform}-${process.arch}`];
}

function runtimePythonPath() {
  return process.platform === "win32"
    ? path.join(drbugHome, "runtime", "python", "python.exe")
    : path.join(drbugHome, "runtime", "python", "bin", "python3");
}

function download(url, destination, redirects = 0) {
  return new Promise((resolve, reject) => {
    if (redirects > 8) {
      reject(new Error("too many redirects while downloading Python runtime"));
      return;
    }
    const request = https.get(url, (response) => {
      if (
        response.statusCode >= 300 &&
        response.statusCode < 400 &&
        response.headers.location
      ) {
        response.resume();
        const next = new URL(response.headers.location, url).toString();
        download(next, destination, redirects + 1).then(resolve, reject);
        return;
      }
      if (response.statusCode !== 200) {
        response.resume();
        reject(
          new Error(`download failed (HTTP ${response.statusCode}): ${url}`),
        );
        return;
      }
      const stream = fs.createWriteStream(destination);
      response.pipe(stream);
      stream.on("finish", () => stream.close(resolve));
      stream.on("error", reject);
    });
    request.on("error", reject);
    request.setTimeout(120000, () => {
      request.destroy(new Error("download timed out"));
    });
  });
}

function sha256File(filename) {
  const hash = crypto.createHash("sha256");
  hash.update(fs.readFileSync(filename));
  return hash.digest("hex");
}

async function ensureRuntimePython() {
  const local = findPython();
  if (local) {
    return local.command;
  }

  const asset = runtimeAsset();
  if (!asset) {
    fail(
      `找不到 Python 3.11+，且当前平台 (${process.platform}-${process.arch}) ` +
        "没有固定的自举运行时；请先安装 Python 3.11+ 后重试",
    );
  }

  if (fs.existsSync(runtimePythonPath())) {
    return runtimePythonPath();
  }

  const runtimeRoot = path.join(drbugHome, "runtime");
  fs.mkdirSync(runtimeRoot, { recursive: true });
  const archive = path.join(runtimeRoot, asset.filename);
  if (!fs.existsSync(archive) || sha256File(archive) !== asset.sha256) {
    note(
      `首次运行需要下载固定版本 Python ${PINNED_PYTHON} ` +
        `(${process.platform}-${process.arch}) 到 ${runtimeRoot}`,
    );
    await download(
      RELEASE_BASE + encodeURIComponent(asset.filename),
      archive,
    );
  }
  const digest = sha256File(archive);
  if (digest !== asset.sha256) {
    fs.rmSync(archive, { force: true });
    fail(
      `Python 运行时校验失败：期望 sha256 ${asset.sha256}，实际 ${digest}`,
    );
  }

  note("正在解压 Python 运行时…");
  const extracted = spawnSync("tar", ["-xzf", archive, "-C", runtimeRoot], {
    stdio: "inherit",
    env: childEnv(),
  });
  if (extracted.error || extracted.status !== 0) {
    fail("解压 Python 运行时失败（需要系统 tar 命令）");
  }
  if (!fs.existsSync(runtimePythonPath())) {
    fail(`解压后仍找不到 ${runtimePythonPath()}`);
  }
  return runtimePythonPath();
}

function venvPythonPath() {
  return process.platform === "win32"
    ? path.join(venvDir, "Scripts", "python.exe")
    : path.join(venvDir, "bin", "python");
}

function requirementsDigest() {
  const requirements = fs.readFileSync(
    path.join(platformRoot, "requirements.txt"),
  );
  const version = require(path.join(packageRoot, "package.json")).version;
  return crypto
    .createHash("sha256")
    .update(requirements)
    .update(`\ndrbug=${version}\n`)
    .digest("hex");
}

function markerMatches(digest) {
  try {
    const marker = JSON.parse(fs.readFileSync(venvMarker, "utf8"));
    return marker.requirements_sha256 === digest && fs.existsSync(venvPythonPath());
  } catch (error) {
    return false;
  }
}

function ensureVenv(python) {
  const digest = requirementsDigest();
  if (markerMatches(digest)) {
    return venvPythonPath();
  }

  note(`首次运行准备 Python 依赖（${venvDir}，仅此一次）…`);
  fs.rmSync(venvDir, { recursive: true, force: true });
  const created = spawnSync(python, ["-m", "venv", venvDir], {
    stdio: "inherit",
    env: childEnv(),
  });
  if (created.error || created.status !== 0) {
    fail(`创建虚拟环境失败（Python: ${python}）`);
  }

  const installed = spawnSync(
    venvPythonPath(),
    [
      "-m",
      "pip",
      "install",
      "--disable-pip-version-check",
      "-r",
      path.join(platformRoot, "requirements.txt"),
    ],
    { stdio: "inherit", env: childEnv() },
  );
  if (installed.error || installed.status !== 0) {
    fail("安装依赖失败；请检查网络后重新运行 drbug");
  }

  fs.writeFileSync(
    venvMarker,
    JSON.stringify(
      {
        requirements_sha256: digest,
        python,
        created_at: new Date().toISOString(),
      },
      null,
      2,
    ) + "\n",
  );
  return venvPythonPath();
}

function bundledRuntime() {
  const runtimeRoot = path.join(platformRoot, "runtime");
  let marker;
  try {
    marker = JSON.parse(
      fs.readFileSync(path.join(runtimeRoot, "bundled.json"), "utf8"),
    );
  } catch (error) {
    return null;
  }
  if (marker.platform !== `${process.platform}-${process.arch}`) {
    return null;
  }
  if (marker.requirements_sha256 !== requirementsDigest()) {
    return null;
  }
  let python =
    process.platform === "win32"
      ? path.join(runtimeRoot, "python", "python.exe")
      : path.join(runtimeRoot, "python", "bin", "python3");
  if (process.platform !== "win32" && !fs.existsSync(python)) {
    // Packaging tools can drop the python3 symlink; the versioned
    // binary (python3.12) is a real file and works the same.
    const binDir = path.join(runtimeRoot, "python", "bin");
    try {
      const candidate = fs
        .readdirSync(binDir)
        .filter((name) => /^python3\.\d+$/.test(name))
        .sort()
        .pop();
      if (candidate) {
        python = path.join(binDir, candidate);
      }
    } catch (error) {
      return null;
    }
  }
  const sitePackages = path.join(runtimeRoot, "site-packages");
  if (!fs.existsSync(python) || !fs.existsSync(sitePackages)) {
    return null;
  }
  return { python, sitePackages };
}

async function main() {
  if (!fs.existsSync(path.join(platformRoot, "core", "cli.py"))) {
    fail("安装包不完整（缺少 platform/core/cli.py），请重新安装 drbug");
  }

  const bundled = bundledRuntime();
  let runPython;
  const pythonPathParts = [];
  if (bundled) {
    runPython = bundled.python;
    pythonPathParts.push(bundled.sitePackages);
  } else {
    const python = await ensureRuntimePython();
    runPython = ensureVenv(python);
  }
  pythonPathParts.push(platformRoot, process.env.PYTHONPATH || "");
  const pythonPath = pythonPathParts
    .filter(Boolean)
    .join(path.delimiter);

  const child = spawnSync(
    runPython,
    ["-m", "core.cli", ...process.argv.slice(2)],
    {
      cwd: process.cwd(),
      env: {
        ...childEnv(),
        PYTHONPATH: pythonPath,
        PYTHONIOENCODING: "utf-8",
        DRBUG_PLATFORM_ROOT: platformRoot,
      },
      stdio: "inherit",
    },
  );

  if (child.error) {
    fail(child.error.message);
  }
  if (child.signal) {
    process.exit(130);
  }
  process.exit(child.status === null ? 1 : child.status);
}

main().catch((error) => fail(error && error.message ? error.message : error));
