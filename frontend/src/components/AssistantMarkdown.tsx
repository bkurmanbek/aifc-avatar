import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { stripSpeechHints } from '../utils'

export function AssistantMarkdown({ text }: { text: string }) {
  return (
    <div className="md-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{stripSpeechHints(text)}</ReactMarkdown>
    </div>
  )
}
