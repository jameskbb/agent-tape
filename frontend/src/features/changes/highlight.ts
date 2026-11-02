/**
 * A small, dependency-free, line-at-a-time highlighter for diff lines.
 *
 * It recognises comments, strings, numbers, and keywords for the languages agents touch
 * most. It never parses across lines (a diff line may start mid-string), so it stays
 * predictable: worst case a token is left uncoloured.
 */

export type TokenKind = "plain" | "keyword" | "string" | "comment" | "number" | "key";

export interface Token {
  kind: TokenKind;
  text: string;
}

interface Lang {
  keywords: ReadonlySet<string>;
  lineComment: string[];
  /** YAML/TOML/JSON keys: `name:` or `"name":` at the start of a line. */
  keys?: boolean;
}

const PY = new Set(
  "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield match case self".split(
    " ",
  ),
);
const JS = new Set(
  "abstract as async await break case catch class const continue debugger default delete do else enum export extends false finally for from function if implements import in instanceof interface let new null of private protected public readonly return static super switch this throw true try type typeof undefined var void while with yield".split(
    " ",
  ),
);
const GO = new Set(
  "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var nil true false".split(
    " ",
  ),
);
const RUST = new Set(
  "as async await break const continue crate else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while".split(
    " ",
  ),
);
const SH = new Set(
  "if then else elif fi for while do done case esac function in export local return set".split(" "),
);
const NONE = new Set<string>();

const LANGS: Record<string, Lang> = {
  py: { keywords: PY, lineComment: ["#"] },
  js: { keywords: JS, lineComment: ["//"] },
  go: { keywords: GO, lineComment: ["//"] },
  rs: { keywords: RUST, lineComment: ["//"] },
  sh: { keywords: SH, lineComment: ["#"] },
  yaml: {
    keywords: new Set(["true", "false", "null", "yes", "no"]),
    lineComment: ["#"],
    keys: true,
  },
  toml: { keywords: new Set(["true", "false"]), lineComment: ["#"], keys: true },
  json: { keywords: new Set(["true", "false", "null"]), lineComment: [], keys: true },
  md: { keywords: NONE, lineComment: [] },
};

const EXT: Record<string, string> = {
  py: "py",
  pyi: "py",
  js: "js",
  jsx: "js",
  ts: "js",
  tsx: "js",
  mjs: "js",
  cjs: "js",
  go: "go",
  rs: "rs",
  sh: "sh",
  bash: "sh",
  zsh: "sh",
  yml: "yaml",
  yaml: "yaml",
  toml: "toml",
  json: "json",
  md: "md",
  markdown: "md",
};

export function languageFor(path: string): string | null {
  const name = path.split("/").pop() ?? path;
  if (name === "Dockerfile" || name === "Makefile") return "sh";
  const dot = name.lastIndexOf(".");
  if (dot < 0) return null;
  return EXT[name.slice(dot + 1).toLowerCase()] ?? null;
}

const WORD = /[A-Za-z_$][\w$]*/y;
const NUMBER = /(?:0x[\da-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)/y;

export function tokenize(line: string, lang: string | null): Token[] {
  const spec = lang ? LANGS[lang] : undefined;
  if (!spec) return [{ kind: "plain", text: line }];
  const out: Token[] = [];
  const push = (kind: TokenKind, text: string) => {
    if (!text) return;
    const last = out[out.length - 1];
    if (last && last.kind === kind) last.text += text;
    else out.push({ kind, text });
  };
  if (spec.keys) {
    const m = /^(\s*-?\s*)("?[\w.-]+"?)(\s*:)/.exec(line);
    if (m && m[1] !== undefined && m[2] && m[3]) {
      push("plain", m[1]);
      push("key", m[2]);
      push("plain", m[3]);
      line = line.slice(m[0].length);
    }
  }
  let i = 0;
  while (i < line.length) {
    const rest = line.slice(i);
    const comment = spec.lineComment.find((c) => rest.startsWith(c));
    if (comment) {
      push("comment", rest);
      break;
    }
    const ch = line[i] ?? "";
    if (ch === '"' || ch === "'" || ch === "`") {
      let j = i + 1;
      while (j < line.length && line[j] !== ch) j += line[j] === "\\" ? 2 : 1;
      push("string", line.slice(i, Math.min(j + 1, line.length)));
      i = j + 1;
      continue;
    }
    WORD.lastIndex = i;
    const word = WORD.exec(line);
    if (word) {
      push(spec.keywords.has(word[0]) ? "keyword" : "plain", word[0]);
      i += word[0].length;
      continue;
    }
    NUMBER.lastIndex = i;
    const num = NUMBER.exec(line);
    if (num && num[0]) {
      push("number", num[0]);
      i += num[0].length;
      continue;
    }
    push("plain", ch);
    i += 1;
  }
  return out;
}
