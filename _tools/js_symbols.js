// Print the top-level symbols of a JS file as JSON (used by app/selfinspect.py).
const fs = require("fs");
const os = require("os");
const path = require("path");

let acorn;
for (const candidate of [
  "acorn",
  path.join(process.env.NPM_GLOBAL || path.join(process.env.USER_HOME || os.homedir(), "npm-global"), "node_modules", "acorn"),
]) {
  try { acorn = require(candidate); break; } catch (e) { /* keep looking */ }
}
if (!acorn) {
  process.stdout.write(JSON.stringify({ __error__: "acorn not installed" }));
  process.exit(0);
}

// Walk every node without pulling in acorn-walk: the shapes we care about are
// few and this keeps the dependency to one package.
function walk(node, visit, parent) {
  if (!node || typeof node.type !== "string") return;
  visit(node, parent);
  for (const key of Object.keys(node)) {
    if (key === "type" || key === "start" || key === "end" || key === "loc") continue;
    const child = node[key];
    if (Array.isArray(child)) {
      for (const item of child) {
        if (item && typeof item.type === "string") walk(item, visit, node);
      }
    } else if (child && typeof child.type === "string") {
      walk(child, visit, node);
    }
  }
}

function analyse(file) {
  let src;
  try {
    src = fs.readFileSync(file, "utf8");
  } catch (e) {
    return { symbols: [], refs: [], error: "unreadable" };
  }
  let tree;
  try {
    tree = acorn.parse(src, {
      ecmaVersion: "latest",
      locations: true,
      allowReturnOutsideFunction: true,
      allowAwaitOutsideFunction: true,
      sourceType: "script",
    });
  } catch (e) {
    // A file that does not parse is not an error here; it is a file this tool
    // cannot answer about, same as the Python side.
    return { symbols: [], refs: [], error: String(e.message || e).slice(0, 160) };
  }
  const lines = src.split(/\r?\n/);
  const symbols = [];
  const refs = [];
  const textAt = (line) => (lines[line - 1] || "").trim().slice(0, 200);

  walk(tree, (node, parent) => {
    const line = node.loc ? node.loc.start.line : 0;
    if (!line) return;

    // ---- definitions
    if (node.type === "FunctionDeclaration" && node.id) {
      symbols.push({ name: node.id.name, kind: "function", line, text: textAt(line) });
    } else if (node.type === "ClassDeclaration" && node.id) {
      symbols.push({ name: node.id.name, kind: "class", line, text: textAt(line) });
    } else if (node.type === "MethodDefinition" && node.key && node.key.name) {
      symbols.push({ name: node.key.name, kind: "method", line, text: textAt(line) });
    } else if (node.type === "VariableDeclarator" && node.id && node.id.name) {
      const init = node.init;
      const isFn = init && (init.type === "FunctionExpression" ||
                            init.type === "ArrowFunctionExpression");
      // `var renderAssistant = function () {}` is a function by any useful
      // definition; an ALL_CAPS binding is the constant it looks like.
      const kind = isFn ? "function"
                        : (node.id.name === node.id.name.toUpperCase() &&
                           /[A-Z]/.test(node.id.name) ? "constant" : "");
      if (kind) symbols.push({ name: node.id.name, kind, line, text: textAt(line) });
    }

    // ---- uses
    if (node.type === "CallExpression") {
      const fn = node.callee;
      if (fn && fn.type === "Identifier") {
        refs.push({ name: fn.name, kind: "call", line, text: textAt(line) });
      } else if (fn && fn.type === "MemberExpression" && fn.property && fn.property.name) {
        refs.push({ name: fn.property.name, kind: "call", line, text: textAt(line) });
      }
    } else if (node.type === "Identifier" && parent &&
               parent.type !== "MemberExpression" &&
               parent.type !== "Property") {
      refs.push({ name: node.name, kind: "reference", line, text: textAt(line) });
    } else if (node.type === "MemberExpression" && node.property && node.property.name) {
      refs.push({ name: node.property.name, kind: "reference", line, text: textAt(line) });
    }
  });

  return { symbols, refs };
}

const out = {};
for (const file of process.argv.slice(2)) {
  out[file.replace(/\\/g, "/")] = analyse(file);
}
process.stdout.write(JSON.stringify(out));
