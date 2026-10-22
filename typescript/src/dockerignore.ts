/** `.dockerignore` matching for the build context.
 * - One pattern per line; blank lines and `#` comments ignored.
 * - Paths are matched relative to the context root, with `/` separators.
 * - `*` and `?` match within one path segment; `**` matches across segments.
 * - Leading and trailing `/` are stripped; directory matches include descendants.
 * - `!` negates, and the last matching pattern wins.
 * - Bare names are anchored to the context root. */

import fs from "node:fs";
import nodePath from "node:path";

export type DockerIgnore = { ignores(relPath: string): boolean };

function patternToRegex(pattern: string): RegExp {
  let out = "^";
  let i = 0;
  while (i < pattern.length) {
    const char = pattern[i]!;
    if (char === "*") {
      if (pattern.slice(i, i + 2) === "**") {
        i += 2;
        if (pattern[i] === "/") {
          // A double-star followed by a slash must also match zero
          // directories, so that `x` alone still matches.
          out += "(?:.*/)?";
          i += 1;
        } else {
          out += ".*";
        }
        continue;
      }
      out += "[^/]*";
    } else if (char === "?") {
      out += "[^/]";
    } else {
      out += char.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }
    i += 1;
  }
  return new RegExp(out + "$");
}

/** Read `<contextRoot>/.dockerignore`. Absent means nothing is excluded. */
export function loadDockerignore(contextRoot: string): DockerIgnore {
  let text: string;
  try {
    text = fs.readFileSync(nodePath.join(contextRoot, ".dockerignore"), "utf8");
  } catch {
    return { ignores: () => false };
  }

  const rules: { regex: RegExp; negated: boolean }[] = [];
  for (const raw of text.split(/\r?\n/)) {
    let line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const negated = line.startsWith("!");
    if (negated) line = line.slice(1).trim();
    line = line.replace(/^\/+/, "").replace(/\/+$/, "");
    if (!line) continue;
    rules.push({ regex: patternToRegex(line), negated });
  }

  return {
    ignores(relPath: string): boolean {
      if (rules.length === 0) return false;

      // Every ancestor is tested too, so a rule naming a directory excludes
      // the files under it without needing a `/**` suffix.
      const candidates = [relPath];
      let parent = nodePath.posix.dirname(relPath);
      while (parent && parent !== "." && parent !== "/") {
        candidates.push(parent);
        parent = nodePath.posix.dirname(parent);
      }

      let ignored = false;
      for (const rule of rules) {
        if (candidates.some((candidate) => rule.regex.test(candidate))) {
          ignored = !rule.negated;
        }
      }
      return ignored;
    },
  };
}
