import { spawn } from 'node:child_process'

import { appendUniquePathEntries, delimiterForPlatform, pathEnvKey } from './backend-env'

// Login-shell PATH resolution for GUI launches.
//
// A desktop app launched from Finder/Dock (macOS launchd) or a desktop
// environment's launcher (Linux) inherits a minimal PATH such as
// /usr/bin:/bin:/usr/sbin:/sbin — none of the user's shell profiles ever run.
// backend-env.ts papers over the common cases with a static sane-entry list
// (Homebrew, /usr/local), but anything only a profile adds — ~/.local/bin,
// nvm/pyenv/asdf shims, ~/.cargo/bin, nix profiles — stays invisible to the
// backend process. That breaks tool availability checks (shutil.which), stdio
// MCP server spawns, and the Electron-side binary resolvers.
//
// Fix (same approach as VS Code's shell-environment resolution; ported from
// cline/cline#12429): run the user's shell once as an interactive login shell,
// capture $PATH between sentinel markers so profile banners can't corrupt the
// value, and merge it into process.env — login-shell entries first (so
// /opt/homebrew/bin wins), current-only entries appended (so dirs injected by
// the launching environment aren't lost).
//
// Failure hardening: a broken/slow shell profile must never brick app startup.
// Every attempt is bounded by a timeout, stdin is closed immediately, and any
// failure leaves PATH untouched.

const PATH_START = '__HERMES_LOGIN_PATH_START__'
const PATH_END = '__HERMES_LOGIN_PATH_END__'
const PROBE_COMMAND = "printf '%s' \"" + PATH_START + '${PATH}' + PATH_END + '"'
const ATTEMPT_TIMEOUT_MS = 5000

function loginShellExecutable(env: any = process.env, platform = process.platform) {
  const shell = typeof env?.SHELL === 'string' ? env.SHELL.trim() : ''

  if (shell) {
    return shell
  }

  // macOS Catalina+ defaults to zsh; most Linux distros default to bash.
  return platform === 'darwin' ? '/bin/zsh' : '/bin/bash'
}

// Extract $PATH from between the sentinel markers. Uses the LAST start marker
// so a profile that echoes the environment (or the command line itself) can't
// poison the capture with an earlier partial match.
function extractSentinelPath(stdout) {
  const text = String(stdout || '')
  const start = text.lastIndexOf(PATH_START)

  if (start === -1) {
    return null
  }

  const valueStart = start + PATH_START.length
  const end = text.indexOf(PATH_END, valueStart)

  if (end === -1) {
    return null
  }

  return text.slice(valueStart, end).trim() || null
}

// Login-shell entries first (Homebrew/version-manager dirs win), then any
// current-only entries appended, duplicates and empties dropped.
function mergeLoginShellPath(loginPath, currentPath, { delimiter = ':' }: any = {}) {
  return appendUniquePathEntries([loginPath, currentPath], { delimiter })
}

// Cancellation seals startup resolution when the app accepts a quit. A canceled
// resolution must not create its fallback shell or mutate PATH afterwards.
let probeLifetime = new AbortController()

function cancelLoginShellPath() {
  probeLifetime.abort()
}

function runProbe(shell, flags, spawnFn, timeoutMs, signal: AbortSignal): Promise<string | null> {
  return new Promise(resolve => {
    let settled = false
    let child: ReturnType<typeof spawn> | undefined
    let timer: ReturnType<typeof setTimeout> | undefined
    let stdout = ''
    let outputBytes = 0

    const collectChild = () => {
      // spawn (unlike execFile) forwards detached:true and creates a private
      // POSIX process group. Collect descendants holding inherited pipes too.
      if (child?.pid) {
        try {
          process.kill(-child.pid, 'SIGKILL')
        } catch (error) {
          if (error?.code !== 'ESRCH') {
            console.warn('[login-shell PATH] process group cleanup failed:', error?.code || 'unknown')
          }

          try {
            child.kill('SIGKILL')
          } catch {
            // The process may already have exited.
          }
        }
      }

      child?.stdin?.destroy?.()
      child?.stdout?.destroy?.()
      child?.stderr?.destroy?.()
    }

    const finish = (value: string | null) => {
      if (settled) {return}
      settled = true
      clearTimeout(timer)
      signal.removeEventListener('abort', cancel)

      try {
        collectChild()
      } finally {
        resolve(value)
      }
    }

    const cancel = () => finish(null)

    if (signal.aborted) {
      finish(null)

      return
    }

    signal.addEventListener('abort', cancel, { once: true })
    // execFile's timeout only sends a signal and may never invoke its callback.
    // Own the wall deadline independently of exit/close and pipe completion.
    timer = setTimeout(() => {
      console.warn('[login-shell PATH] probe wall deadline exceeded:', timeoutMs)
      cancel()
    }, timeoutMs)

    try {
      child = spawnFn(shell, [...flags, PROBE_COMMAND], { detached: true, windowsHide: true })
      child.on('error', cancel)
      child.on('close', () => finish(extractSentinelPath(stdout)))
      child.stdout?.setEncoding('utf8')
      child.stdout?.on('data', data => {
        if (settled) {return}
        outputBytes += Buffer.byteLength(data)

        // Preserve the previous execFile output bound without retaining banners
        // indefinitely. Overflow cannot authenticate a partial sentinel.
        if (outputBytes > 1024 * 1024) {
          cancel()

          return
        }

        stdout += data
      })
      child.stderr?.resume()
      child.stdin?.end()
    } catch {
      finish(null)
    }
  })
}

async function captureLoginShellPath({
  env = process.env,
  platform = process.platform,
  spawnFn = spawn,
  timeoutMs = ATTEMPT_TIMEOUT_MS
}: any = {}) {
  if (platform === 'win32') {
    // GUI apps on Windows inherit the user PATH from the registry env block;
    // the launchd-minimal-PATH problem is POSIX-only.
    return null
  }

  const signal = probeLifetime.signal
  const shell = loginShellExecutable(env, platform)

  // -l sources ~/.zprofile / ~/.profile (where `brew shellenv` lives); -i
  // sources ~/.zshrc / ~/.bashrc (where nvm/pyenv-style managers live). Some
  // shells swallow combined -ilc with a non-tty stdin (macOS system bash 3.2
  // — see tests/tools/test_find_shell.py), so fall back to a plain login
  // shell before giving up.
  for (const flags of [['-ilc'], ['-lc']]) {
    if (signal.aborted) {return null}
    const captured = await runProbe(shell, flags, spawnFn, timeoutMs, signal)

    if (captured) {
      return captured
    }
  }

  return null
}

async function applyLoginShellPath({
  env = process.env,
  platform = process.platform,
  spawnFn = spawn,
  timeoutMs = ATTEMPT_TIMEOUT_MS
}: any = {}) {
  if (platform === 'win32') {
    return { applied: false, reason: 'win32' }
  }

  const loginPath = await captureLoginShellPath({ env, platform, spawnFn, timeoutMs })

  if (!loginPath || probeLifetime.signal.aborted) {
    return { applied: false, reason: 'unresolved' }
  }

  const key = pathEnvKey(env, platform)
  const delimiter = delimiterForPlatform(platform)
  const merged = mergeLoginShellPath(loginPath, env?.[key] || '', { delimiter })

  if (!merged || merged === env?.[key]) {
    return { applied: false, reason: 'unchanged', path: merged }
  }

  env[key] = merged

  return { applied: true, path: merged }
}

// Single-flight: the warmup at app start and the await before the backend
// spawn share one resolution. Never rejects.
let _ensurePromise: Promise<any> | null = null

function ensureLoginShellPath(options: any = {}) {
  if (!_ensurePromise) {
    _ensurePromise = applyLoginShellPath(options).catch(error => ({
      applied: false,
      reason: String(error?.message || error)
    }))
  }

  return _ensurePromise
}

function resetLoginShellPathForTests() {
  cancelLoginShellPath()
  probeLifetime = new AbortController()
  _ensurePromise = null
}

export {
  applyLoginShellPath,
  cancelLoginShellPath,
  captureLoginShellPath,
  ensureLoginShellPath,
  extractSentinelPath,
  loginShellExecutable,
  mergeLoginShellPath,
  resetLoginShellPathForTests
}
