// 执行官方协议函数，输出真实帧（对照我们的实现）
const fs = require("fs");
const funcs = fs.readFileSync("_funcs.txt", "utf8");

const stub = `
var h = () => {};
var nr = () => ({
  deviceInfo: { motorNum: 1, prefix: "08", isXht: false, isDualBluetooth: false },
  deviceArray: [],
  allFeatList: [],
});
var te = { state: { device: { curr: { motorNum: 1, prefix: "08" }, deviceArray: [] } } };
var iu = () => null;
`;

const mod = stub + funcs + `
module.exports = { Ce, nt, Oe, Pe, bt };
`;
fs.writeFileSync("_proto.js", mod);
const P = require("./_proto.js");

console.log("=== Ce(cmd, vals) 官方帧构造 ===");
for (const c of ["06", "08", "0a"]) {
  for (const v of [[50], [50, 50], [4, 6, 5]]) {
    console.log(`  Ce("${c}", [${v}]) = ${P.Ce(c, v)}`);
  }
}

console.log("\n=== nt(码表, 双人选, 马达数, 是否停止) ===");
for (const e of [[4, 6, 5], [1], [8], [50], [80, 90]]) {
  console.log(`  nt([${e}], [], 1, false) = ${JSON.stringify(P.nt(e, [], 1, false))}`);
  console.log(`  nt([${e}], [1], 1, false) = ${JSON.stringify(P.nt(e, [1], 1, false))}`);
  console.log(`  nt([${e}], [], 1, true)  = ${JSON.stringify(P.nt(e, [], 1, true))}`);
}
console.log("  nt([80,90], [], 2, false) = " + JSON.stringify(P.nt([80, 90], [], 2, false)));

console.log("\n=== Oe(强度, 选中, 马达数, 停止) — 手动页强度映射 ===");
for (const v of [0, 25, 50, 75, 100, 200, 400]) {
  console.log(`  Oe(${v}, [1], 1, false) = ${JSON.stringify(P.Oe(v, [1], 1, false))}`);
}

console.log("\n=== 手动 50% 在 motorNum=1/prefix=08 下的完整帧 ===");
const arr = P.nt(P.Oe(50, [1], 1, false), [], 1, false);
console.log("  Oe ->", JSON.stringify(P.Oe(50, [1], 1, false)), " nt ->", JSON.stringify(arr),
            " Ce('08', arr) ->", P.Ce("08", arr));

console.log("\n=== writeSpecialMode 13 个索引（固件模式表）===");
const modes = ["aa0A0101b6", "aa0A0102b7", "aa0A0103b8", "aa0A0104b9", "aa0A0105BA",
  "aa0A0106bb", "aa0A0107bc", "aa060101b2", "aa060100b1", "aa0a0100b5",
  "aa0A0108bd", "aa0A0109be", "aa0A010abf"];
modes.forEach((f, i) => console.log(`  [${String(i).padStart(2)}] ${f}`));
