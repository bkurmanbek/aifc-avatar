import type { StructuredAnswer } from '../types'

export function formatStructuredAnswer(answer: StructuredAnswer): string {
  const blocks: string[] = []
  const seen = new Set<string>()

  const pushBlock = (raw: string) => {
    const value = raw.trim()
    if (!value) return
    const key = value.replace(/\s+/g, ' ').trim().toLowerCase()
    if (seen.has(key)) return
    seen.add(key)
    blocks.push(value)
  }

  const summary = answer.details.summary?.trim()
  if (summary) pushBlock(summary)

  for (const section of answer.details.sections) {
    const title = section.title?.trim()
    const text = section.text?.trim()
    const items = (section.items ?? []).map((item) => item.trim()).filter(Boolean)
    const sectionParts: string[] = []
    const hasBody = Boolean(text && text !== summary) || items.length > 0

    if (title && title.toLowerCase() !== 'details' && hasBody) {
      sectionParts.push(`### ${title}`)
    }
    if (items.length > 0) {
      sectionParts.push(items.map((item) => `- ${item}`).join('\n'))
    }
    if (text && text !== summary) {
      sectionParts.push(text)
    }
    if (sectionParts.length > 0) {
      pushBlock(sectionParts.join('\n'))
    }
  }

  return blocks.join('\n\n').trim() || answer.spoken
}
