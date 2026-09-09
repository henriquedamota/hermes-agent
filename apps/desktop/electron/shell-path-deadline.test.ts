import assert from 'node:assert/strict'
import { type ChildProcess, spawn } from 'node:child_process'
import { EventEmitter, once } from 'node:events'
import { PassThrough } from 'node:stream'

import { afterEach, beforeEach, test, vi } from 'vitest'

import * as shellPath from './shell-path'

const START = '__HERMES_LOGIN_PATH_START__'
const END = '__HERMES_LOGIN_PATH_END__'

function fakeChild() {
  return Object.assign(new EventEmitter(), {
    stdin: new PassThrough(),
    stdout: new PassThrough(),
    stderr: new PassThrough()
  })
}

beforeEach(() => shellPath.resetLoginShellPathForTests())
afterEach(() => {
  shellPath.cancelLoginShellPath?.()
  vi.useRealTimers()
})

test.runIf(process.platform !== 'win32')('wall deadline settles even when the shell never emits close', async () => {
  vi.useFakeTimers()
  const env = { SHELL: '/bin/sh', PATH: '/inherited/path' }
  const callbacks: Array<(error: null, stdout: string) => void> = []
  const end = vi.fn()

  const spawnFn = () => {
    const child = fakeChild()
    child.stdin.end = end
    callbacks.push((_error, stdout) => {
      child.stdout.write(stdout)
      child.emit('close', 0)
    })

    return child
  }

  let result: any = null
  void shellPath.ensureLoginShellPath({ env, spawnFn, timeoutMs: 100 }).then(value => {
    result = value
  })
  await vi.advanceTimersByTimeAsync(200)
  assert.deepEqual(result, { applied: false, reason: 'unresolved' })
  assert.equal(callbacks.length, 2)
  assert.equal(end.mock.calls.length, 2)

  for (const callback of callbacks) {callback(null, `${START}/late/path${END}`)}
  await vi.advanceTimersByTimeAsync(0)
  assert.equal(env.PATH, '/inherited/path')
})

test.runIf(process.platform !== 'win32')(
  'a timed-out interactive probe can still use the successful login fallback',
  async () => {
    vi.useFakeTimers()
    const env = { SHELL: '/bin/sh', PATH: '/inherited/path' }
    const flags: string[] = []

    const spawnFn = (_file, args) => {
      flags.push(args[0])
      const child = fakeChild()

      if (args[0] === '-lc')
        {queueMicrotask(() => {
          child.stdout.write(`${START}/login/path${END}`)
          child.emit('close', 0)
        })}

      return child
    }

    let result: any = null
    void shellPath.ensureLoginShellPath({ env, spawnFn, timeoutMs: 100 }).then(value => {
      result = value
    })
    await vi.advanceTimersByTimeAsync(100)
    assert.equal(result?.applied, true)
    assert.deepEqual(flags, ['-ilc', '-lc'])
    assert.equal(env.PATH, '/login/path:/inherited/path')
  }
)

test.runIf(process.platform !== 'win32')(
  'accepted shutdown cancels the probe and prevents fallback and late PATH mutation',
  async () => {
    const env = { SHELL: '/bin/sh', PATH: '/inherited/path' }
    const callbacks: Array<(error: null, stdout: string) => void> = []

    const spawnFn = () => {
      const child = fakeChild()
      callbacks.push((_error, stdout) => {
        child.stdout.write(stdout)
        child.emit('close', 0)
      })

      return child
    }

    const result = shellPath.ensureLoginShellPath({ env, spawnFn })
    shellPath.cancelLoginShellPath()
    assert.equal((await result).applied, false)
    callbacks[0](null, `${START}/late/path${END}`)
    await Promise.resolve()
    assert.equal(callbacks.length, 1)
    assert.equal(env.PATH, '/inherited/path')
    assert.equal((await shellPath.ensureLoginShellPath({ env, spawnFn })).applied, false)
  }
)

test.runIf(process.platform !== 'win32').each(['deadline', 'shutdown'] as const)(
  'real stubborn process group is collected on %s',
  async mode => {
    const children: ChildProcess[] = []
    const descendants: number[] = []
    let cleaning = false
    const exited: Promise<unknown>[] = []

    const script = `
    const { spawn } = require('node:child_process');
    process.on('SIGTERM', () => {});
    const child = spawn(process.execPath, ['-e', 'process.on("SIGTERM",()=>{});setInterval(()=>{},1000)'], { stdio: 'inherit' });
    console.log('CHILD_PID=' + child.pid);
    setInterval(() => {}, 1000);
  `

    const spawnFn = (_file, _args, options) => {
      if (cleaning) {throw new Error('fixture already closed')}
      const child = spawn(process.execPath, ['-e', script], options)
      children.push(child)
      exited.push(once(child, 'exit'))
      child.stdout?.on('data', data => {
        for (const match of String(data).matchAll(/CHILD_PID=(\d+)/g)) {descendants.push(Number(match[1]))}

        if (mode === 'shutdown' && descendants.length) {shellPath.cancelLoginShellPath()}
      })

      return child
    }

    let watchdog: ReturnType<typeof setTimeout> | undefined

    try {
      const result = await Promise.race([
        shellPath.applyLoginShellPath({ env: { PATH: '/inherited/path' }, spawnFn, timeoutMs: 2000 }),
        new Promise<never>((_resolve, reject) => {
          watchdog = setTimeout(() => reject(new Error('probe failed to honor its wall deadline')), 10000)
        })
      ])

      assert.equal(result.applied, false)
      const expected = mode === 'deadline' ? 2 : 1
      assert.equal(children.length, expected)
      assert.equal(descendants.length, expected, 'every fixture tree reached the ready signal')
      await Promise.all(exited)
      // OS reaping is asynchronous; poll actual liveness within a generous bound.
      await vi.waitFor(
        () => {
          for (const pid of descendants) {assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' })}
        },
        { timeout: 5000, interval: 50 }
      )
    } finally {
      cleaning = true
      clearTimeout(watchdog)
      shellPath.cancelLoginShellPath()

      for (const pid of descendants) {
        try {
          process.kill(pid, 'SIGKILL')
        } catch {
            // The process may already have exited.
          }
      }

      for (const child of children) {
        child.kill('SIGKILL')
        child.stdout?.destroy()
        child.stderr?.destroy()
      }
    }
  },
  20000
)

test.runIf(process.platform !== 'win32')('oversized output cannot authenticate a truncated sentinel', async () => {
  const env = { PATH: '/inherited/path' }

  const spawnFn = () => {
    const child = fakeChild()
    queueMicrotask(() => {
      child.stdout.write(`${START}/untrusted/path${END}` + 'x'.repeat(1024 * 1024))
      child.emit('close', 0)
    })

    return child
  }

  const result = await shellPath.applyLoginShellPath({ env, spawnFn })
  assert.equal(result.applied, false)
  assert.equal(env.PATH, '/inherited/path')
})
