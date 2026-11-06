// Prints a video's spoken language, or saves its captions in that language.
//   node save_captions.mjs VIDEO_ID           prints the language code
//   node save_captions.mjs VIDEO_ID OUTPUT    saves a timestamped transcript
import { writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const PARAGRAPH_SECONDS = 60;

// The language comes from the original audio stream. Caption order cannot give it:
// a dubbed video lists a track per language, and a Spanish song listed English first.
// The YouTube Data API's defaultAudioLanguage is worse: wrong or absent on 4 of 8 videos.
export function spokenLanguageTrack(tracks, audioFormats) {
  if (tracks.length === 0) throw new Error('This video has no captions');
  const codes = tracks.map((track) => track.language_code).join(', ');
  const language = audioFormats.find((format) => format.is_original && format.language)?.language;
  if (!language) {
    if (tracks.length === 1) return tracks[0];
    throw new Error(`YouTube does not report the spoken language; captions: ${codes}`);
  }
  const base = (code) => code.split('-')[0].toLowerCase();
  const matches = tracks.filter((track) => base(track.language_code) === base(language));
  if (matches.length === 0) throw new Error(`No captions in the spoken language ${language}; captions: ${codes}`);
  return matches.find((track) => track.kind !== 'asr') ?? matches[0];
}

export function formatTranscript(title, videoId, trackName, events) {
  const url = `https://www.youtube.com/watch?v=${videoId}`;
  const lines = [`# ${title}`, '', `- Video: ${url}`, `- Captions: ${trackName}, unedited`, ''];
  let start = null;
  let words = [];
  const flush = () => {
    lines.push(`[${clock(start)}](${url}&t=${start}s) ${words.join(' ')}`, '');
    start = null;
    words = [];
  };
  for (const event of events) {
    const text = (event.segs ?? []).map((seg) => seg.utf8 ?? '').join('').replace(/\s+/g, ' ').trim();
    if (!text || event.tStartMs === undefined) continue;
    const seconds = Math.floor(event.tStartMs / 1000);
    start ??= seconds;
    words.push(text);
    if (seconds - start >= PARAGRAPH_SECONDS) flush();
  }
  if (words.length > 0) flush();
  return `${lines.join('\n').trimEnd()}\n`;
}

function clock(seconds) {
  const pad = (value) => String(value).padStart(2, '0');
  return `${pad(Math.floor(seconds / 60))}:${pad(seconds % 60)}`;
}

async function main([videoId, output]) {
  const config = process.env.CLAUDE_CONFIG_DIR ?? join(homedir(), '.claude');
  const library = join(config, 'mcp-servers/youtube/app/node_modules/youtubei.js/dist/src/platform/node.js');
  const { Innertube } = await import(library);
  const youtube = await Innertube.create();
  const info = await youtube.getBasicInfo(videoId, { client: 'ANDROID_VR' });
  const track = spokenLanguageTrack(
    info.captions?.caption_tracks ?? [],
    info.streaming_data?.adaptive_formats ?? [],
  );
  if (!output) {
    console.log(track.language_code);
    return;
  }
  const url = new URL(track.base_url);
  url.searchParams.set('fmt', 'json3');
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Caption request failed with HTTP ${response.status}`);
  const { events = [] } = await response.json();
  const name = track.name?.text ?? track.language_code;
  writeFileSync(output, formatTranscript(info.basic_info.title, videoId, name, events));
  console.log(`Saved ${name} captions to ${output}`);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  if (process.argv.length < 3) {
    console.error('Usage: save_captions.mjs VIDEO_ID [OUTPUT]');
    process.exit(2);
  }
  main(process.argv.slice(2)).catch((error) => {
    console.error(error.message);
    process.exit(1);
  });
}
