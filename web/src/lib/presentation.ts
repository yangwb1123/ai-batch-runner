import type { ProblemKind } from './api'

export const problems: Record<ProblemKind, { title: string; detail: string }> = {
  login: { title: '先登录，再查看你的工作现场', detail: '当前数据不可用：尚未登录或本地会话已过期。使用 Snaplink 登录后，服务端会重新核验你的只读授权。' },
  forbidden: { title: '已到达服务，但当前账号没有读取权限', detail: '当前数据不可用：登录不等于项目授权。请联系实例管理员核对租户、客户端、账号与实例范围，再刷新。' },
  limited: { title: '读取请求暂时受限', detail: '当前数据不可用：服务正在限流。请稍后手动刷新；不会自动重放登录或请求。' },
  unavailable: { title: '暂时无法读取工作现场', detail: '当前数据不可用：网络或 Hub 服务没有正常响应。旧数据已清除，请检查服务后重试。' },
  invalid: { title: '服务返回的数据不符合读取契约', detail: '当前数据不可用：响应格式或大小校验未通过。没有把异常内容当成成功或继续显示旧数据。' },
  timeout: { title: '本次读取已超时', detail: '当前数据不可用：完整读取窗口超过 12 秒，已取消请求并清除旧记录。你可以手动重试。' },
}

const statuses: Record<string, string> = {
  idle: '空闲', running: '执行中', queued: '排队中', completed: '执行完成', failed: '执行失败',
  cancelled: '已取消', cancel_requested: '已请求取消', closed: '已关闭', closing: '等待关闭确认',
  created: '会话已创建', lost: '失联 / 所有者已换代', interrupted: '执行已中断',
}
export const sessionLabel = (value: string) => statuses[value] ?? (value || '未声明')
export const heartbeat = (value: boolean | null) => value === true ? '心跳在线' : value === false ? '心跳离线' : '心跳未知'
export function localTime(value: number | null, milliseconds = false): string {
  if (value === null) return '未读取'
  const clock = { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false } as const
  const calendar = { year: 'numeric', month: '2-digit', day: '2-digit', ...clock } as const
  return new Intl.DateTimeFormat('zh-CN', milliseconds ? clock : calendar).format(value * (milliseconds ? 1 : 1000))
}
