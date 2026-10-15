// Format helpers without regular expressions (AGENTS.md §3).
const WHITESPACE = new Set([' ', '\t', '\n', '\r', '\f', '\v', ' ', '　']);
const HEX = new Set('0123456789abcdefABCDEF');

export function collapseWhitespace(text: string): string {
  return Array.from(text, c => (WHITESPACE.has(c) ? ' ' : c)).join('').split(' ').filter(Boolean).join(' ');
}

export function isHex(text: string, length: number): boolean {
  return text.length === length && Array.from(text).every(c => HEX.has(c));
}
