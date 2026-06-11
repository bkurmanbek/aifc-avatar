import { createWriteStream, existsSync, mkdirSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn } from 'node:child_process'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '..', '..')
const logsDir = resolve(root, 'logs')
const logFiles = [
  'backend.log',
  'frontend.log',
  'stt.log',
  'llm.log',
  'tts.log',
  'avatar.log',
  'pipeline.log',
  'websocket.log',
  'errors.log',
]

if (!['0', 'false', 'no', 'off'].includes(String(process.env.RESET_LOGS_ON_START ?? 'true').toLowerCase())) {
  mkdirSync(logsDir, { recursive: true })
  for (const entry of readdirSync(logsDir, { withFileTypes: true })) {
    rmSync(resolve(logsDir, entry.name), { recursive: true, force: true })
  }
  for (const file of logFiles) {
    writeFileSync(resolve(logsDir, file), '')
  }
}

mkdirSync(logsDir, { recursive: true })
const frontendLog = createWriteStream(resolve(logsDir, 'frontend.log'), { flags: 'a' })

const viteBin = resolve(here, '..', 'node_modules', '.bin', process.platform === 'win32' ? 'vite.cmd' : 'vite')
const command = existsSync(viteBin) ? viteBin : 'vite'
const child = spawn(command, process.argv.slice(2), {
  cwd: resolve(here, '..'),
  env: process.env,
  stdio: ['inherit', 'pipe', 'pipe'],
})

child.on('error', (error) => {
  const line = `failed to start vite: ${error.stack ?? error.message}\n`
  process.stderr.write(line)
  frontendLog.write(line)
  frontendLog.end()
  process.exit(1)
})

child.stdout.on('data', (chunk) => {
  process.stdout.write(chunk)
  frontendLog.write(chunk)
})

child.stderr.on('data', (chunk) => {
  process.stderr.write(chunk)
  frontendLog.write(chunk)
})

child.on('close', (code, signal) => {
  frontendLog.end()
  if (signal) {
    process.kill(process.pid, signal)
    return
  }
  process.exit(code ?? 0)
})
