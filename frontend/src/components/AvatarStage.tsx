import { useRef } from 'react'
import type { RefObject } from 'react'
import { CANVAS_W, CANVAS_H } from '../constants'

const IDLE_VIDEO_SRC = (import.meta.env.VITE_IDLE_VIDEO_SRC as string | undefined)?.trim() || '/idle.mp4?v=4'

interface AvatarStageProps {
  mode: string
  micEnabled: boolean
  activeListening: boolean
  isListening: boolean
  isBusy: boolean
  showComposer: boolean
  followUpQuestions: string[]
  showFollowUps: boolean
  showTranscript: boolean
  fullscreenTargetRef: RefObject<HTMLDivElement | null>
  idleVideoRef: RefObject<HTMLVideoElement | null>
  speakCanvasRef: RefObject<HTMLCanvasElement | null>
  onToggleMic: () => void
  onToggleMute: () => void
  onInterrupt: () => void
  onToggleComposer: () => void
  onSelectFollowUp: (question: string) => void
}

export function AvatarStage({
  mode,
  micEnabled,
  activeListening: _activeListening,
  isListening,
  isBusy,
  showComposer,
  followUpQuestions: _followUpQuestions,
  showFollowUps: _showFollowUps,
  showTranscript: _showTranscript,
  fullscreenTargetRef,
  idleVideoRef,
  speakCanvasRef,
  onToggleMic,
  onToggleMute,
  onInterrupt,
  onToggleComposer,
  onSelectFollowUp: _onSelectFollowUp,
}: AvatarStageProps) {
  const stageRef = useRef<HTMLDivElement | null>(null)
  const primaryAction = isBusy && !isListening ? onInterrupt : onToggleMic
  const toggleFullscreen = () => {
    if (document.fullscreenElement) {
      void document.exitFullscreen()
      return
    }
    void (fullscreenTargetRef.current ?? stageRef.current)?.requestFullscreen()
  }

  return (
    <section className="avatar-col" aria-label="AI avatar stage">
      <div className={`stage ${mode}`} ref={stageRef}>
        <div className="stage-presence" aria-hidden="true" />
        <video ref={idleVideoRef} id="idleVid" autoPlay loop muted playsInline src={IDLE_VIDEO_SRC} />
        <canvas ref={speakCanvasRef} id="speakCvs" width={CANVAS_W} height={CANVAS_H} />
        {/* Follow-up suggestions disabled
        {!showComposer && !showTranscript && showFollowUps && followUpQuestions.length > 0 && (
          <div className="stage-followup-strip" aria-label="Suggested follow-up questions">
            {followUpQuestions.slice(0, 3).map((question) => (
              <button key={question} className="stage-followup-card" type="button" onClick={() => onSelectFollowUp(question)}>
                {question}
              </button>
            ))}
          </div>
        )}
        */}
        <div className="video-control-dock" aria-label="Video controls">
          <button className={`video-ctrl ${showComposer ? 'active' : ''}`} type="button" onClick={onToggleComposer} aria-pressed={showComposer} aria-label={showComposer ? 'Hide text input' : 'Show text input'}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M21 15a2 2 0 0 1-2 2H8l-5 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
            </svg>
          </button>
          <button
            className={`video-ctrl video-ctrl-primary ${isListening ? 'listening' : ''} ${isBusy && !isListening ? 'danger' : ''}`}
            type="button"
            onClick={primaryAction}
            disabled={!micEnabled && !isBusy}
            aria-label={isListening ? 'Stop recording' : isBusy ? 'Interrupt response' : 'Start speaking'}
          >
            {isBusy && !isListening ? (
              <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                <rect x="7" y="7" width="10" height="10" rx="2" />
              </svg>
            ) : (
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
                <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
                <path d="M12 19v4" />
                <path d="M8 23h8" />
              </svg>
            )}
          </button>
          <button
            className={`video-ctrl ${!micEnabled ? 'muted' : 'active'}`}
            type="button"
            onClick={onToggleMute}
            aria-pressed={!micEnabled}
            aria-label={micEnabled ? 'Mute microphone input' : 'Unmute microphone input'}
          >
            {micEnabled ? (
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M11 5 6 9H2v6h4l5 4V5z" />
                <path d="M15.5 8.5a5 5 0 0 1 0 7" />
                <path d="M19 5a9 9 0 0 1 0 14" />
              </svg>
            ) : (
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path d="M11 5 6 9H2v6h4l5 4V5z" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                <line x1="22" y1="2" x2="2" y2="22" stroke="#ef4444" strokeWidth="2.2" strokeLinecap="round" />
              </svg>
            )}
          </button>
          <button className="video-ctrl" type="button" onClick={toggleFullscreen} aria-label="Fullscreen">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M8 3H5a2 2 0 0 0-2 2v3" />
              <path d="M16 3h3a2 2 0 0 1 2 2v3" />
              <path d="M8 21H5a2 2 0 0 1-2-2v-3" />
              <path d="M16 21h3a2 2 0 0 0 2-2v-3" />
            </svg>
          </button>
        </div>
      </div>
    </section>
  )
}
