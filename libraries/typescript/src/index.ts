// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * StandIn SDK - put your AI agent into a Microsoft Teams call.
 *
 * StandIn (https://standin.komaa.com) is the hosted bridge that joins the Microsoft Teams
 * call: it owns the Microsoft side entirely - the bot registration, Graph, media
 * negotiation, the avatar tile - and speaks to your worker over one
 * authenticated socket per call. This package is that socket's other end.
 *
 * The Python SDK (`standin-sdk` on PyPI) is the same API, same seam, same wire
 * protocol. Which one you use is decided by the framework you are integrating,
 * not by preference.
 *
 * This module is the CORE, and it is the whole reason one package works: it
 * imports nothing from `plugins/`, so `import { CallServer } from
 * "@komaa/standin-sdk"` succeeds on a machine with no framework installed at
 * all. A plugin is reached by its own subpath - `@komaa/standin-sdk/echo`,
 * `@komaa/standin-sdk/openclaw` - and only a caller that already has that
 * framework ever loads it. Keep it that way: one framework import at the top of
 * a file reachable from here would break every install that does not use it.
 *
 * You do not normally use this directly. Reach for the plugin for your
 * framework; the root-level examples show a complete deployment of each.
 *
 * Writing a plugin is the other direction, and it is deliberately small -
 * see {@link CallHandler}, or copy `plugins/echo`, which answers a real
 * call in under 100 lines.
 */

export {
  BYTES_PER_SAMPLE,
  FRAME_BYTES,
  FRAME_MS,
  REALTIME_SAMPLE_RATE_HZ,
  FrameAligner,
  frameDurationMs,
  pcm16Rms,
  resamplePcm16,
} from "./audio.js";
export {
  EMOTIONS,
  ExpressionCue,
  MAX_EMOTION_CHARS,
  MAX_VISEME_ID,
  type Emotion,
  type InferredEmotion,
  type SpeechMark,
  expression,
  inferEmotion,
  speechMarks,
} from "./avatar.js";
export {
  MAX_AUDIO_BUFFER_BYTES,
  CallServer,
  type CallServerOptions,
} from "./callServer.js";
export {
  TROUBLE_ANSWERING,
  TROUBLE_HEARING,
  TROUBLE_SPEAKING,
  VoiceLane,
  type Answer,
  type Synthesize,
  type Transcribe,
  type VoiceLaneOptions,
  type VoiceTurn,
} from "./lane.js";
export {
  SyntheticCall,
  runSmoke,
  report as smokeReport,
  type SmokeCheck,
  type SmokeResult,
} from "./smoke.js";
export {
  LiveCalls,
  TENANT_ENV,
  VoiceDelivery,
  type Delivery,
  type LiveSpeaker,
  type VoiceDeliveryOptions,
} from "./delivery.js";
export {
  BUILT_IN_TOOLS,
  SHOW_PAGE_TOOL,
  CallTools,
  type CallToolsOptions,
  type ToolDialect,
  type ToolHandler,
  type ToolResult,
  type ToolSpec,
  toolSchemas,
} from "./callTools.js";
export { flag, jsonObject, optional, required, vendorHost } from "./config.js";
export {
  BACKGROUND_TASK_TOOL,
  CONSULT_TOOL,
  DEFAULT_CONSULT_TIMEOUT_MS,
  DEFAULT_RESUME_LIMIT,
  DEFAULT_TASK_TIMEOUT_MS,
  DEFAULT_TASK_TTL_MS,
  BackgroundTasks,
  Consultant,
  type Asker,
  type AskerFactory,
  type BackgroundTask,
  type BackgroundTasksOptions,
  type Deliverer,
} from "./consult.js";
export {
  CHAT_FALLBACK_WINDOW_MS,
  PersonalChats,
  type PersonalChat,
  DEFAULT_CHAT_URL,
  OUTBOUND_IMAGE_CONTENT_TYPES,
  OUTBOUND_IMAGE_MAX_BYTES,
  SCHEMA_VERSION,
  ChatChannel,
  type ChatChannelOptions,
  type InboundMessage,
  type OutboundImage,
  buildReply,
  isPersonal,
  outboundImage,
  parseInbound,
  sanitizeImageName,
  sniffImageType,
} from "./chat.js";
export {
  ECHO_BARGE_IN_RMS,
  ECHO_SUPPRESSION_WINDOW_MS,
  pcm16Rms as echoPcm16Rms,
  shouldSuppressEcho,
  type EchoGuardOptions,
} from "./echoGuard.js";
export { StandInError } from "./errors.js";
export {
  DEFAULT_FOLLOW_UP_WINDOW_MS,
  GroupGate,
  type GateDecision,
  type GroupGateOptions,
  isAddressed,
  isMeetingThread,
  isVerbalInterrupt,
} from "./gate.js";
export {
  describeInboundRejection,
  isAllowlistedCaller,
  isInboundCallAllowed,
  normalizePhoneNumber,
} from "./policy.js";
export {
  type ForbiddenIpFn,
  type LookupFn,
  assertPublicHttpUrl,
  fetchPublicImage,
  isForbiddenIp,
} from "./fetch.js";
export type { CallHandler, CallSession, HandlerFactory } from "./handler.js";
export {
  CHAT_REPLAY_WINDOW_MS,
  REPLAY_WINDOW_MS,
  SIGNATURE_HEADER,
  SIGNATURE_V2_HEADER,
  TIMESTAMP_HEADER,
  canonicalRequest,
  nowMs,
  signBody,
  signHandshake,
  signRequest,
  verifyBody,
  verifyHandshake,
} from "./hmac.js";
export {
  ATTACHMENT_NOTE_MAX_LINES,
  CARD_PAYLOAD_MAX_CHARS,
  CLIP_FETCH_ATTEMPTS,
  CLIP_FETCH_TIMEOUT_MS,
  IMAGE_FETCH_ATTEMPTS,
  IMAGE_FETCH_TIMEOUT_MS,
  MAX_CLIPS,
  MAX_CLIP_BYTES,
  MAX_IMAGES,
  type ChatAudio,
  type ChatImage,
  type ChatTurn,
  type ChatTurnOptions,
  type FetchOptions,
  type Transcriber,
  attachmentsNote,
  buildChatTurn,
  cardActionNote,
  chatAttachmentOrigin,
  chatImageData,
  chatImageDataUrl,
  fetchChatAudio,
  fetchChatImages,
  spoolClip,
  transcribeVoiceMessages,
} from "./attachments.js";
export {
  AMBIENT_BACKSTOP_MS,
  AMBIENT_SOURCE_ORDER,
  DEFAULT_AMBIENT_MAX_PER_MINUTE,
  MAX_QUEUED_AMBIENT_IMAGES,
  AmbientVision,
  type AmbientImage,
  type AmbientSession,
  type AmbientSink,
  type AmbientVisionOptions,
  ambientImageDataUrl,
} from "./ambient.js";
export { type Logger, setLogger } from "./log.js";
export {
  MEDIA_ROOTS_ENV,
  type AgentMedia,
  type LoadMediaOptions,
  loadMedia,
  mediaRoots,
  parseMedia,
} from "./media.js";
export {
  DEFAULT_MAX_UTTERANCE_MS,
  DEFAULT_MIN_UTTERANCE_MS,
  DEFAULT_PREROLL_MS,
  DEFAULT_SILENCE_MS,
  DEFAULT_SPEECH_RMS,
  PacedPlayback,
  UtteranceSegmenter,
  type FrameSink,
  type Playback,
  type SegmenterOptions,
  decodeWav,
  encodeWav,
} from "./voice.js";
export {
  DOCUMENT_NOT_ATTACHED,
  MAX_TRANSCRIPT_CHARS,
  MAX_TRANSCRIPT_ENTRIES,
  MAX_TRANSCRIPT_ENTRY_CHARS,
  MAX_TRANSCRIPT_TURNS,
  MAX_TRANSCRIPT_VISUALS,
  MINUTES_TOOL,
  RECAP_MIN_TURNS,
  Transcript,
  type DeliveryTarget,
  type MinutesSection,
  type MinutesTargetOptions,
  type PostMinutesOptions,
  type PostOutcome,
  type Poster,
  type RecapResult,
  type Summariser,
  type TranscriptOptions,
  type Turn,
  type TurnRole,
  hasSpeakerPrefix,
  isSummaryRequest,
  minutesPrompt,
  parseMinutesSections,
  postMinutes,
  resolveMinutesTarget,
  writeMinutesDocx,
} from "./minutes.js";
export {
  CHAR_VISEMES,
  SILENCE_VISEME,
  TurnLipSync,
  estimateVisemes,
  visemeForChar,
  visemesFromAlignment,
  type TurnLipSyncOptions,
} from "./lipsync.js";
export {
  OutboundCaller,
  OutboundError,
  OutboundPolicy,
  PendingMessages,
  type OutboundCallerOptions,
  type OutboundPolicyOptions,
  type PendingMessage,
  type PlacedCall,
  stateDir,
  CALL_BACK_TOOL,
  CHAT_CALLBACK_TOOL,
  CHAT_CALLBACK_WINDOW_MS,
  DEFAULT_ANSWER_TIMEOUT_MS,
  MAX_PENDING_TEXT_CHARS,
  NO_ANSWER_PREFIX,
  OUTCOME_WORDING,
  UNANSWERED_OUTCOMES,
  OutboundLane,
  OutboundLeg,
  type ChatCallbackTarget,
  type ChatSender,
  type OutboundLaneOptions,
  type OutboundSession,
  type Speak,
  callThreadIsPostable,
} from "./outbound.js";
export {
  NUM_CHANNELS,
  SAMPLE_RATE_HZ,
  type Caller,
  type SessionStart,
  assistantCancel,
  audioFrame,
  contextSentences,
  decodePcm,
  parseMessage,
  parseSessionStart,
  pong,
  sessionEnd,
} from "./protocol.js";
export {
  MAX_TILE_FPS,
  TILE_HEIGHT,
  TILE_WIDTH,
  TileStream,
  type Encoder,
  type TileStreamOptions,
  jpegEncoder,
} from "./tile.js";
export {
  DISPLAY_MODES,
  KeyframeStore,
  PAGE_DISPLAY_MS,
  PAGE_RENDER_TIMEOUT_MS,
  MAX_SLIDESHOW_IMAGES,
  SLIDESHOW_HOLD_MS,
  SLIDESHOW_OVERLAP_MS,
  ShownImage,
  VisionBudget,
  VisionTools,
  displayImageName,
  normalizeDisplayMode,
  type PageRenderer,
  type ShowItem,
  type Speaker,
  type VisionToolsOptions,
  type WalkthroughStep,
} from "./visionTools.js";
export {
  MAX_PENDING_AUDIO,
  MAX_PENDING_CONTEXT,
  StartupBuffer,
} from "./startup.js";
export { VERSION } from "./version.js";
export {
  DISPLAY_IMAGE_MIME_TYPES,
  MAX_IMAGE_BYTES,
  VIDEO_SOURCES,
  FrameDescriber,
  type FrameDescriberOptions,
  type DisplayFrameOptions,
  type DisplayImageMime,
  type DisplayImageMode,
  type DisplayImageOptions,
  type VideoFrame,
  type VideoSource,
  displayFrame,
  displayImage,
  parseVideoFrame,
  fallbackOwner,
  frameCaption,
  frameDigest,
  frameOwner,
} from "./vision.js";
