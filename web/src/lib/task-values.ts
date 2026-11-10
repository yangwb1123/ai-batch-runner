import { ReadProblem } from './api'

const CONTROL = /[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/
const TEXT_CONTROL = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g
export const TURN_STATES = ['queued', 'running', 'cancel_requested', 'cancelled', 'completed', 'failed', 'interrupted', 'lost']

export function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ReadProblem('invalid')
  return value as Record<string, unknown>
}
export function taskId(value: unknown): string {
  if (typeof value !== 'string' || !value || value.length > 256 || value.trim() !== value || CONTROL.test(value) || /[/\\]/.test(value) || ['.', '..'].includes(value)) throw new ReadProblem('invalid')
  return value
}
export function boolean(value: unknown): boolean {
  if (typeof value !== 'boolean') throw new ReadProblem('invalid')
  return value
}
export function member(value: unknown, choices: string[]): string {
  if (typeof value !== 'string' || !choices.includes(value)) throw new ReadProblem('invalid')
  return value
}
export function boundedText(value: unknown, limit = 16384): string {
  if (typeof value !== 'string' || new TextEncoder().encode(value).length > limit) throw new ReadProblem('invalid')
  return value.replace(TEXT_CONTROL, ' ')
}
export function cursor(value: unknown, before = ''): string {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{0,2048}$/.test(value) || (value && value === before)) throw new ReadProblem('invalid')
  return value
}
export function timestamp(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value >= 1e11) throw new ReadProblem('invalid')
  return value
}
