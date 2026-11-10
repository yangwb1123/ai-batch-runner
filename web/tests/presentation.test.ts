import { expect, test } from 'bun:test'
import { heartbeat, localTime, sessionLabel } from '../src/lib/presentation'

test('unknown heartbeat is not offline, and old record times retain the calendar date', () => {
  expect(heartbeat(null)).toBe('心跳未知')
  expect(heartbeat(false)).toBe('心跳离线')
  expect(heartbeat(true)).toBe('心跳在线')
  expect(localTime(null)).toBe('未读取')
  expect(localTime(1700000000)).toContain('2023')
  expect(sessionLabel('completed')).toBe('执行完成') // never claims independent acceptance
})
