/**
 * A Camoufox Pro profile's browser state, sealed on this machine: which files
 * of a Firefox profile travel, how they are cut into chunks, sealed with the
 * account's own key, and listed in a manifest, and how a manifest is written
 * back out as a profile directory. Nothing here talks to the API; see
 * pro-profile.ts. TypeScript twin of pythonlib/camoufox/pro_state.py; both
 * pass the same vectors (docs/pro.md, "Profiles").
 */
import { createHash, createHmac, hkdfSync, randomBytes } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import * as zlib from "node:zlib";
import { xchacha20poly1305 } from "@noble/ciphers/chacha.js";

// ── chunking: FastCDC 2020, gear "g1" ───────────────────────────────────────

export const CHUNKING = {
	algo: "fastcdc",
	min: 262144,
	avg: 1048576,
	max: 4194304,
	gear: "g1",
} as const;

/** The first 8 bytes of SHA-256("cfp/fastcdc-gear/g1"), big-endian, top bit cleared. */
export const GEAR_SEED = 0x47fb985c9b393779n;

/** FastCDC 2020's gear table (fastcdc-rs v2020, pyfastcdc). */
const GEAR = [
	"3b5d3c7d207e37dc",
	"784d68ba91123086",
	"cd52880f882e7298",
	"eacf8e4e19fdcca7",
	"c31f385dfbd1632b",
	"1d5f27001e25abe6",
	"83130bde3c9ad991",
	"c4b225676e9b7649",
	"aa329b29e08eb499",
	"b67fcbd21e577d58",
	"0027baaada2acf6b",
	"e3ef2d5ac73c2226",
	"0890f24d6ed312b7",
	"a809e036851d7c7e",
	"f0a6fe5e0013d81b",
	"1d026304452cec14",
	"03864632648e248f",
	"cdaacf3dcd92b9b4",
	"f5e012e63c187856",
	"8862f9d3821c00b6",
	"a82f7338750f6f8a",
	"1e583dc6c1cb0b6f",
	"7a3145b69743a7f1",
	"abb20fee404807eb",
	"b14b3cfe07b83a5d",
	"b9dc27898adb9a0f",
	"3703f5e91baa62be",
	"cf0bb866815f7d98",
	"3d9867c41ea9dcd3",
	"1be1fa65442bf22c",
	"14300da4c55631d9",
	"e698e9cbc6545c99",
	"4763107ec64e92a5",
	"c65821fc65696a24",
	"76196c064822f0b7",
	"485be841f3525e01",
	"f652bc9c85974ff5",
	"cad8352face9e3e9",
	"2a6ed1dceb35e98e",
	"c6f483badc11680f",
	"3cfd8c17e9cf12f1",
	"89b83c5e2ea56471",
	"ae665cfd24e392a9",
	"ec33c4e504cb8915",
	"3fb9b15fc9fe7451",
	"d7fd1fd1945f2195",
	"31ade0853443efd8",
	"255efc9863e1e2d2",
	"10eab6008d5642cf",
	"46f04863257ac804",
	"a52dc42a789a27d3",
	"daaadf9ce77af565",
	"6b479cd53d87febb",
	"6309e2d3f93db72f",
	"c5738ffbaa1ff9d6",
	"6bd57f3f25af7968",
	"67605486d90d0a4a",
	"e14d0b9663bfbdae",
	"b7bbd8d816eb0414",
	"def8a4f16b35a116",
	"e7932d85aaaffed6",
	"08161cbae90cfd48",
	"855507beb294f08b",
	"91234ea6ffd399b2",
	"ad70cf4b2435f302",
	"d289a97565bc2d27",
	"8e558437ffca99de",
	"96d2704b7115c040",
	"0889bbcdfc660e41",
	"5e0d4e67dc92128d",
	"72a9f8917063ed97",
	"438b69d409e016e3",
	"df4fed8a5d8a4397",
	"00f41dcf41d403f7",
	"4814eb038e52603f",
	"9dafbacc58e2d651",
	"fe2f458e4be170af",
	"4457ec414df6a940",
	"06e62f1451123314",
	"bd1014d173ba92cc",
	"def318e25ed57760",
	"9fea0de9dfca8525",
	"459de1e76c20624b",
	"aeec189617e2d666",
	"126a2c06ab5a83cb",
	"b1321532360f6132",
	"65421503dbb40123",
	"2d67c287ea089ab3",
	"6c93bff5a56bd6b6",
	"4ffb2036cab6d98d",
	"ce7b785b1be7ad4f",
	"edb42ef6189fd163",
	"dc905288703988f6",
	"365f9c1d2c691884",
	"c640583680d99bfe",
	"3cd4624c07593ec6",
	"7f1ea8d85d7c5805",
	"014842d480b57149",
	"0b649bcb5a828688",
	"bcd5708ed79b18f0",
	"e987c862fbd2f2f0",
	"982731671f0cd82c",
	"baf13e8b16d8c063",
	"8ea3109cbd951bba",
	"d141045bfb385cad",
	"2acbc1a0af1f7d30",
	"e6444d89df03bfdf",
	"a18cc771b8188ff9",
	"9834429db01c39bb",
	"214add07fe086a1f",
	"8f07c19b1f6b3ff9",
	"56a297b1bf4ffe55",
	"94d558e493c54fc7",
	"40bfc24c764552cb",
	"931a706f8a8520cb",
	"32229d322935bd52",
	"2560d0f5dc4fefaf",
	"9dbcc48355969bb6",
	"0fd81c3985c0b56a",
	"e03817e1560f2bda",
	"c1bb4f81d892b2d5",
	"b0c4864f4e28d2d7",
	"3ecc49f9d9d6c263",
	"51307e99b52ba65e",
	"8af2b688da84a752",
	"f5d72523b91b20b6",
	"6d95ff1ff4634806",
	"562f21555458339a",
	"c0ce47f889336346",
	"487823e5089b40d8",
	"e4727c7ebc6d9592",
	"5a8f7277e94970ba",
	"fca2f406b1c8bb50",
	"5b1f8a95f1791070",
	"d304af9fc9028605",
	"5440ab7fc930e748",
	"312d25fbca2ab5a1",
	"10f4a4b234a4d575",
	"90301d55047e7473",
	"3b6372886c61591e",
	"293402b77c444e06",
	"451f34a4d3e97dd7",
	"3158d814d81bc57b",
	"034942425b9bda69",
	"e2032ff9e532d9bb",
	"62ae066b8b2179e5",
	"9545e10c2f8d71d8",
	"7ff7483eb2d23fc0",
	"00945fcebdc98d86",
	"8764bbbe99b26ca2",
	"1b1ec62284c0bfc3",
	"58e0fcc4f0aa362b",
	"5f4abefa878d458d",
	"fd74ac2f9607c519",
	"a4e3fb37df8cbfa9",
	"bf697e43cac574e5",
	"86f14a3f68f4cd53",
	"24a23d076f1ce522",
	"e725cd8048868cc8",
	"bf3c729eb2464362",
	"d8f6cd57b3cc1ed8",
	"6329e52425541577",
	"62aa688ad5ae1ac0",
	"0a242566269bf845",
	"168b1a4753aca74b",
	"f789afefff2e7e3c",
	"6c3362093b6fccdb",
	"4ce8f50bd28c09b2",
	"006a2db95ae8aa93",
	"975b0d623c3d1a8c",
	"18605d3935338c5b",
	"5bb6f6136cad3c71",
	"0f53a20701f8d8a6",
	"ab8c5ad2e7e93c67",
	"40b5ac5127acaa29",
	"8c7bf63c2075895f",
	"78bd9f7e014a805c",
	"b2c9e9f4f9c8c032",
	"efd6049827eb91f3",
	"2be459f482c16fbd",
	"d92ce0c5745aaa8c",
	"0aaa8fb298d965b9",
	"2b37f92c6c803b15",
	"8c54a5e94e0f0e78",
	"95f9b6e90c0a3032",
	"e7939faa436c7874",
	"d16bfe8f6a8a40c9",
	"44982b86263fd2fa",
	"e285fb39f984e583",
	"779a8df72d7619d3",
	"f2d79a8de8d5dd1e",
	"d1037354d66684e2",
	"004c82a4e668a8e5",
	"31d40a7668b044e6",
	"d70578538bd02c11",
	"db45431078c5f482",
	"977121bb7f6a51ad",
	"73d5ccbd34eff8dd",
	"e437a07d356e17cd",
	"47b2782043c95627",
	"9fb251413e41d49a",
	"ccd70b60652513d3",
	"1c95b31e8a1b49b2",
	"cae73dfd1bcb4c1b",
	"34d98331b1f5b70f",
	"784e39f22338d92f",
	"18613d4a064df420",
	"f1d8dae25f0bcebe",
	"33f77c15ae855efc",
	"3c88b3b912eb109c",
	"956a2ec96bafeea5",
	"1aa005b5e0ad0e87",
	"5500d70527c4bb8e",
	"e36c57196421cc44",
	"13c4d286cc36ee39",
	"5654a23d818b2a81",
	"77b1dc13d161abdc",
	"734f44de5f8d5eb5",
	"60717e174a6c89a2",
	"d47d9649266a211e",
	"5b13a4322bb69e90",
	"f7669609f8b5fc3c",
	"21e6ac55bedcdac9",
	"9b56b62b61166dea",
	"f48f66b939797e9c",
	"35f332f9c0e6ae9a",
	"cc733f6a9a878db0",
	"3da161e41cc108c2",
	"b7d74ae535914d51",
	"4d493b0b11d36469",
	"ce264d1dfba9741a",
	"a9d1f2dc7436dc06",
	"70738016604c2a27",
	"231d36e96e93f3d5",
	"7666881197838d19",
	"4a2a83090aaad40c",
	"f1e761591668b35d",
	"7363236497f730a7",
	"301080e37379dd4d",
	"502dea2971827042",
	"c2c5eb858f32625f",
	"786afb9edfafbdff",
	"daee0d868490b2a4",
	"617366b3268609f6",
	"ae0e35a0fe46173e",
	"d1a07de93e824f11",
	"079b8b115ea4cca8",
	"93a99274558faebb",
	"fb1e6e22e08a03b3",
	"ea635fdba3698dd0",
	"cf53659328503a5c",
	"cde3b31e6fd5d780",
	"8e3e4221d3614413",
	"ef14d0d86bf1a22c",
	"e1d830d3f16c5ddb",
	"aabd2b2a451504e1",
];

const U64 = (1n << 64n) - 1n;
const hiLo = (values: bigint[]) => ({
	hi: Uint32Array.from(values, (v) => Number(v >> 32n)),
	lo: Uint32Array.from(values, (v) => Number(v & 0xffffffffn)),
});
/** The g1 tables: every entry XORed with the seed, the shifted table with the shifted seed. */
export const G1 = hiLo(GEAR.map((hex) => BigInt(`0x${hex}`) ^ GEAR_SEED));
export const G1_LS = hiLo(
	GEAR.map(
		(hex) => ((BigInt(`0x${hex}`) << 1n) & U64) ^ ((GEAR_SEED << 1n) & U64),
	),
);
// Normalization level 2 around 2^20: MASKS[22] and MASKS[18], and each shifted once.
const MASK_S = 0x0000d93767537000n;
const MASK_L = 0x0000d90707537000n;
const mask = (m: bigint) => [Number(m >> 32n), Number(m & 0xffffffffn)];
const [MS_HI, MS_LO] = mask(MASK_S);
const [ML_HI, ML_LO] = mask(MASK_L);
const [MSLS_HI, MSLS_LO] = mask(MASK_S << 1n);
const [MLLS_HI, MLLS_LO] = mask(MASK_L << 1n);

/**
 * The 64-bit gear hash, kept as two 32-bit halves so the loop never allocates.
 * Returns the cut offset within [from, to), or -1 when there is none.
 */
function scan(
	buf: Uint8Array,
	state: Uint32Array,
	start: number,
	from: number,
	to: number,
	lsHi: number,
	lsLo: number,
	mHi: number,
	mLo: number,
): number {
	let hi = state[0];
	let lo = state[1];
	for (let pos = from; pos < to; pos += 2) {
		let b = buf[start + pos];
		hi = ((hi << 2) | (lo >>> 30)) >>> 0;
		lo = (lo << 2) >>> 0;
		let sum = lo + G1_LS.lo[b];
		lo = sum >>> 0;
		hi = (hi + G1_LS.hi[b] + (sum > 0xffffffff ? 1 : 0)) >>> 0;
		if ((hi & lsHi) === 0 && (lo & lsLo) === 0) return pos;
		b = buf[start + pos + 1];
		sum = lo + G1.lo[b];
		lo = sum >>> 0;
		hi = (hi + G1.hi[b] + (sum > 0xffffffff ? 1 : 0)) >>> 0;
		if ((hi & mHi) === 0 && (lo & mLo) === 0) return pos + 1;
	}
	state[0] = hi;
	state[1] = lo;
	return -1;
}

function cut(buf: Uint8Array, start: number): number {
	let remaining = buf.length - start;
	if (remaining <= CHUNKING.min) return remaining;
	let center: number = CHUNKING.avg;
	if (remaining > CHUNKING.max) remaining = CHUNKING.max;
	else if (remaining < center) center = remaining;
	const state = new Uint32Array(2);
	const first = CHUNKING.min & ~1;
	const mid = center & ~1;
	const end = remaining & ~1;
	const small = scan(
		buf,
		state,
		start,
		first,
		mid,
		MSLS_HI,
		MSLS_LO,
		MS_HI,
		MS_LO,
	);
	if (small >= 0) return small;
	const large = scan(
		buf,
		state,
		start,
		Math.max(first, mid),
		end,
		MLLS_HI,
		MLLS_LO,
		ML_HI,
		ML_LO,
	);
	return large >= 0 ? large : remaining;
}

/** The chunk lengths FastCDC g1 cuts `buf` into. */
export function chunkLengths(buf: Uint8Array): number[] {
	const lengths: number[] = [];
	for (let offset = 0; offset < buf.length; ) {
		const length = cut(buf, offset);
		lengths.push(length);
		offset += length;
	}
	return lengths;
}

// ── keys and sealing ────────────────────────────────────────────────────────

const NONCE_BYTES = 24;
const ZSTD_LEVEL = 3;

function be64(value: number): Buffer {
	const out = Buffer.alloc(8);
	out.writeBigUInt64BE(BigInt(value));
	return out;
}

function be32(value: number): Buffer {
	const out = Buffer.alloc(4);
	out.writeUInt32BE(value);
	return out;
}

const PROFILE_ID =
	/^prf_([0-9a-f]{8})-([0-9a-f]{4})-(7[0-9a-f]{3})-([89ab][0-9a-f]{3})-([0-9a-f]{12})$/;

/** The 16 raw bytes of the UUID in a `prf_` id. */
export function uuid16(profileId: string): Buffer {
	const match = PROFILE_ID.exec(profileId);
	if (!match) throw new Error(`not a prf_ UUIDv7 profile id: ${profileId}`);
	return Buffer.from(match.slice(1).join(""), "hex");
}

function zstd(data: Uint8Array): Buffer {
	return zlib.zstdCompressSync(data, {
		params: { [zlib.constants.ZSTD_c_compressionLevel]: ZSTD_LEVEL },
	});
}

/** A sealed object that fails authentication, decompression or its id check. */
export class StateIntegrityError extends Error {
	name = "StateIntegrityError";
}

export function chunkAad(accountId: number, chunkId: Uint8Array): Buffer {
	return Buffer.concat([Buffer.from("cfp/chunk/1"), be64(accountId), chunkId]);
}

export function manifestAad(
	accountId: number,
	profileId: string,
	version: number,
): Buffer {
	return Buffer.concat([
		Buffer.from("cfp/manifest/1"),
		be64(accountId),
		uuid16(profileId),
		be32(version),
	]);
}

/** An account's state keys, derived from its content key (K_acct). */
export class AccountKeys {
	private constructor(
		readonly accountId: number,
		readonly kId: Buffer,
		readonly kChunk: Buffer,
		readonly kMan: Buffer,
	) {}

	static derive(kAcct: Uint8Array, accountId: number): AccountKeys {
		if (kAcct.length !== 32) {
			throw new Error(`a content key is 32 bytes, got ${kAcct.length}`);
		}
		const key = (info: string) =>
			Buffer.from(hkdfSync("sha256", kAcct, Buffer.alloc(0), info, 32));
		return new AccountKeys(
			accountId,
			key("cfp/chunk-id/1"),
			key("cfp/chunk-enc/1"),
			key("cfp/manifest-enc/1"),
		);
	}

	chunkId(plaintext: Uint8Array): Buffer {
		return createHmac("sha256", this.kId).update(plaintext).digest();
	}

	sealChunk(chunkId: Uint8Array, plaintext: Uint8Array): Buffer {
		return seal(
			this.kChunk,
			zstd(plaintext),
			chunkAad(this.accountId, chunkId),
		);
	}

	/** Decrypt, decompress and recompute the id, so a chunk swapped in from elsewhere never restores. */
	openChunk(chunkId: Uint8Array, blob: Uint8Array, size: number): Buffer {
		const compressed = open(
			this.kChunk,
			blob,
			chunkAad(this.accountId, chunkId),
			"chunk",
		);
		let plaintext: Buffer;
		try {
			plaintext = zlib.zstdDecompressSync(compressed, {
				maxOutputLength: Math.max(size, 1),
			});
		} catch (error) {
			throw new StateIntegrityError(
				`chunk does not decompress to ${size} bytes: ${(error as Error).message}`,
			);
		}
		if (
			plaintext.length !== size ||
			!this.chunkId(plaintext).equals(Buffer.from(chunkId))
		) {
			throw new StateIntegrityError("chunk plaintext does not match its id");
		}
		return plaintext;
	}

	sealManifest(
		profileId: string,
		version: number,
		document: Uint8Array,
	): Buffer {
		return seal(
			this.kMan,
			zstd(document),
			manifestAad(this.accountId, profileId, version),
		);
	}

	openManifest(profileId: string, version: number, blob: Uint8Array): Buffer {
		const compressed = open(
			this.kMan,
			blob,
			manifestAad(this.accountId, profileId, version),
			"manifest",
		);
		return zlib.zstdDecompressSync(compressed);
	}
}

/** nonce || XChaCha20-Poly1305(key, nonce, plaintext, aad). */
export function seal(
	key: Uint8Array,
	plaintext: Uint8Array,
	aad: Uint8Array,
	nonce: Uint8Array = randomBytes(NONCE_BYTES),
): Buffer {
	return Buffer.concat([
		nonce,
		xchacha20poly1305(key, nonce, aad).encrypt(plaintext),
	]);
}

function open(
	key: Uint8Array,
	blob: Uint8Array,
	aad: Uint8Array,
	what: string,
): Buffer {
	try {
		return Buffer.from(
			xchacha20poly1305(key, blob.subarray(0, NONCE_BYTES), aad).decrypt(
				blob.subarray(NONCE_BYTES),
			),
		);
	} catch {
		throw new StateIntegrityError(`${what} failed authentication`);
	}
}

// ── the manifest ────────────────────────────────────────────────────────────

export const MANIFEST_FORMAT = "cfp-state-manifest/1";

export interface ChunkRef {
	id: string;
	size: number;
	stored: number;
}

export interface FileEntry {
	path: string;
	size: number;
	mode: number;
	mtime: number;
	chunks: ChunkRef[];
}

export interface Manifest {
	format: string;
	profile_id: string;
	version: number;
	base_version: number;
	captured_at: string;
	crashed: boolean;
	integrity: "ok" | "suspect";
	suspect_files: string[];
	ff_version: string;
	driver_version: string;
	policy_version: string;
	chunking: Record<string, string | number>;
	files: FileEntry[];
	dirs: string[];
	total_bytes: number;
	file_count: number;
	chunk_count: number;
	sha256: string;
}

/** Sorted keys, no whitespace: Python's json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False). */
export function canonical(value: unknown): string {
	if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
	if (value !== null && typeof value === "object") {
		return `{${Object.keys(value)
			.sort()
			.map(
				(key) =>
					`${JSON.stringify(key)}:${canonical((value as Record<string, unknown>)[key])}`,
			)
			.join(",")}}`;
	}
	if (typeof value === "number" && !Number.isSafeInteger(value)) {
		throw new Error(`${value} is not a JSON-safe integer`);
	}
	return JSON.stringify(value);
}

const sha256Hex = (data: string | Uint8Array) =>
	createHash("sha256").update(data).digest("hex");

export function selfHash(document: Record<string, unknown>): string {
	return sha256Hex(canonical({ ...document, sha256: "" }));
}

type ManifestFields = Omit<
	Manifest,
	"format" | "total_bytes" | "file_count" | "chunk_count" | "sha256"
>;

/** The complete manifest for these fields: totals, counts and the self-hash filled in. */
export function manifestOf(fields: ManifestFields): Manifest {
	const distinct = new Set(
		fields.files.flatMap((entry) => entry.chunks.map((ref) => ref.id)),
	);
	const document: Manifest = {
		format: MANIFEST_FORMAT,
		...fields,
		total_bytes: fields.files.reduce((sum, entry) => sum + entry.size, 0),
		file_count: fields.files.length,
		chunk_count: distinct.size,
		sha256: "",
	};
	document.sha256 = selfHash(document as unknown as Record<string, unknown>);
	return document;
}

function checkRelpath(relpath: string): void {
	const parts = relpath.split("/");
	if (
		relpath.startsWith("/") ||
		parts.some((part) => part === "" || part === "." || part === "..") ||
		relpath.includes("\\") ||
		relpath.includes("\0")
	) {
		throw new Error(
			`manifest path ${JSON.stringify(relpath)} is not a plain relative path`,
		);
	}
}

/** Parse a decrypted manifest, refusing one whose self-hash, identity or counts do not hold. */
export function decodeManifest(
	data: Uint8Array,
	profileId: string,
	version: number,
): Manifest {
	const document = JSON.parse(Buffer.from(data).toString("utf-8"));
	if (document.format !== MANIFEST_FORMAT) {
		throw new Error(`not a ${MANIFEST_FORMAT} document: ${document.format}`);
	}
	if (document.sha256 !== selfHash(document)) {
		throw new Error("manifest self-hash does not match its contents");
	}
	if (document.profile_id !== profileId || document.version !== version) {
		throw new Error(
			`manifest is ${document.profile_id} v${document.version}, expected ${profileId} v${version}`,
		);
	}
	const { format, total_bytes, file_count, chunk_count, sha256, ...fields } =
		document;
	if (canonical(manifestOf(fields)) !== canonical(document)) {
		throw new Error(
			"manifest totals or fields are inconsistent with its file list",
		);
	}
	for (const entry of document.files as FileEntry[]) {
		checkRelpath(entry.path);
		for (const ref of entry.chunks) chunkIdBytes(ref.id);
		if (entry.chunks.reduce((sum, ref) => sum + ref.size, 0) !== entry.size) {
			throw new Error(
				`${entry.path}: chunk sizes do not add up to the file size`,
			);
		}
	}
	for (const dir of document.dirs as string[]) checkRelpath(dir);
	return document;
}

export const chunkIdText = (id: Uint8Array) =>
	Buffer.from(id).toString("base64url");

export function chunkIdBytes(text: string): Buffer {
	const raw = Buffer.from(text, "base64url");
	if (raw.length !== 32 || chunkIdText(raw) !== text) {
		throw new Error(`malformed chunk id ${JSON.stringify(text)}`);
	}
	return raw;
}

// ── which files travel (state policy sp/2) ──────────────────────────────────

export const POLICY_VERSION = "sp/2";
export const HARD_CAP = 1 << 30;
const DEFAULT_RULE_MAX = 1 << 20;
/** Files the launcher writes into every profile itself. */
const DRIVER_OWNED = ["user.js", "motor.json"];

const INCLUDED_FILES = new Set([
	"cookies.sqlite",
	"places.sqlite",
	"favicons.sqlite",
	"storage.sqlite",
	"storage/ls-archive.sqlite",
	"sessionstore.jsonlz4",
	"key4.db",
	"cert9.db",
	"pkcs11.txt",
	"logins.json",
	"logins.db",
	"permissions.sqlite",
	"content-prefs.sqlite",
	"formhistory.sqlite",
	"webappsstore.sqlite",
	"protections.sqlite",
	"bounce-tracking-protection.sqlite",
	"notificationstore.json",
	"serviceworker.txt",
	"SiteSecurityServiceState.bin",
	"AlternateServices.bin",
	"prefs.js",
	"extensions.json",
	"extension-preferences.json",
	"extension-settings.json",
	"addons.json",
	"handlers.json",
	"containers.json",
	"search.json.mozlz4",
	"xulstore.json",
	"times.json",
	"signedInUser.json",
]);
const INCLUDED_TREES = [
	"storage/default",
	"storage/permanent",
	"sessionstore-backups",
	"extensions",
	"extension-store",
];
const EXCLUDED_ROOT_DIRS = [
	"cache2",
	"startupCache",
	"thumbnails",
	"shader-cache",
	"jumpListCache",
	"OfflineCache",
	"gmp*",
	"safebrowsing",
	"remote-settings",
	"settings",
	"security_state",
	"datareporting",
	"crashes",
	"minidumps",
	"saved-telemetry-pings",
	"bookmarkbackups",
];
const EXCLUDED_NESTED_DIRS = ["storage/temporary", "extensions/staged"];
const LOCK_FILES = ["lock", ".parentlock", "parent.lock"];
const EXCLUDED_ROOT_FILES = [
	"suggest.sqlite",
	"domain_to_categories.sqlite",
	"Telemetry*.json",
	"ExperimentStoreData.json",
	"shield-preference-experiments.json",
	"activity-stream.*.json",
	"compatibility.ini",
	"addonStartup.json.lz4",
	"sessionCheckpoints.json",
	".startup-incomplete",
	...LOCK_FILES,
	...DRIVER_OWNED,
];
const EXCLUDED_ANY_DEPTH = [
	"*.sqlite-wal",
	"*.sqlite-shm",
	"*.sqlite-journal",
	"*-corrupt",
	"*.tmp",
];
/** The Nimbus store is root-only: IndexedDB files under storage/ can have the same shape of name. */
const NIMBUS_STORE = /^[0-9a-f]{8}\.sqlite$/;

/** Prefs the launcher sets on every launch, and prefs naming a path on this host. */
const MANAGED_PREF_PREFIXES = [
	"webgl.",
	"font.name-list.",
	"font.size.",
	"browser.cache.",
	"browser.sessionhistory.",
	"browser.tabs.remote.useCrossOriginOpenerPolicy",
	"dom.webgpu.enabled",
	"dom.w3c_touch_events.enabled",
	"gfx.bundled-fonts.activate",
	"javascript.options.use_ucrt_for_sin_cos_tan",
	"media.holo-",
	"media.peerconnection.enabled",
	"network.dns.disableIPv6",
	"network.http.http2.timeout",
	"network.http.keep-alive.timeout",
	"network.http.max-connections",
	"permissions.default.image",
	"ui.use_standins_for_native_colors",
];
const HOST_PATH_PREFS = new Set([
	"browser.download.dir",
	"browser.download.lastDir",
	"print_printer",
]);

const globCache = new Map<string, RegExp>();
function glob(pattern: string): RegExp {
	let re = globCache.get(pattern);
	if (!re) {
		re = new RegExp(
			`^${pattern
				.replace(/[.+^${}()|[\]\\]/g, "\\$&")
				.replaceAll("*", ".*")
				.replaceAll("?", ".")}$`,
			"s",
		);
		globCache.set(pattern, re);
	}
	return re;
}
const matches = (name: string, patterns: string[]) =>
	patterns.some((pattern) => glob(pattern).test(name));

function dirExcluded(relpath: string): boolean {
	return (
		(!relpath.includes("/") && matches(relpath, EXCLUDED_ROOT_DIRS)) ||
		EXCLUDED_NESTED_DIRS.includes(relpath)
	);
}

function underExcludedDir(relpath: string): boolean {
	const parts = relpath.split("/");
	for (let depth = 1; depth < parts.length; depth++) {
		if (dirExcluded(parts.slice(0, depth).join("/"))) return true;
	}
	return false;
}

function fileExcluded(relpath: string): boolean {
	const name = relpath.slice(relpath.lastIndexOf("/") + 1);
	if (underExcludedDir(relpath) || matches(name, EXCLUDED_ANY_DEPTH)) {
		return true;
	}
	return (
		!relpath.includes("/") &&
		(matches(name, EXCLUDED_ROOT_FILES) || NIMBUS_STORE.test(name))
	);
}

/** Whether a file of this path and size travels. */
export function fileIncluded(relpath: string, size: number): boolean {
	if (fileExcluded(relpath)) return false;
	if (
		INCLUDED_FILES.has(relpath) ||
		INCLUDED_TREES.some((tree) => relpath.startsWith(`${tree}/`))
	) {
		return true;
	}
	return !relpath.includes("/") && size < DEFAULT_RULE_MAX;
}

/** A directory Firefox may expect even when empty: inside an included tree or on the way to one. */
function dirKept(relpath: string): boolean {
	if (dirExcluded(relpath) || underExcludedDir(relpath)) return false;
	return INCLUDED_TREES.some(
		(tree) =>
			relpath === tree ||
			relpath.startsWith(`${tree}/`) ||
			tree.startsWith(`${relpath}/`),
	);
}

const USER_PREF = /^\s*user_pref\(\s*"([^"]*)"\s*,\s*(.*)\)\s*;\s*$/s;
// prefs.js escapes a backslash as two, so `C:\` is written `C:\\`.
const HOST_PATH_VALUE = /^"(\/|[A-Za-z]:\\\\)/;

/** Drop managed and host-bound user_pref lines; every other byte is kept. */
export function filterPrefs(text: Buffer): Buffer {
	const lines = text.toString("latin1").split("\n");
	return Buffer.from(
		lines
			.filter((line) => {
				const match = USER_PREF.exec(line);
				if (!match) return true;
				const name = match[1];
				return !(
					MANAGED_PREF_PREFIXES.some((prefix) => name.startsWith(prefix)) ||
					HOST_PATH_PREFS.has(name) ||
					HOST_PATH_VALUE.test(match[2])
				);
			})
			.join("\n"),
		"latin1",
	);
}

// ── capture ─────────────────────────────────────────────────────────────────

export interface CapturedFile {
	path: string;
	size: number;
	mode: number;
	mtime: number;
}

export interface Snapshot {
	root: string;
	files: CapturedFile[];
	dirs: string[];
	suspectFiles: string[];
}

export class StateTooLarge extends Error {
	name = "StateTooLarge";
}

/**
 * Fold a database's pending write-ahead log into it and check it. False means
 * the database must travel as it is, with its -wal and -shm.
 */
async function checkpoint(file: string): Promise<boolean> {
	const family = [file, `${file}-wal`, `${file}-shm`].filter((member) =>
		fs.existsSync(member),
	);
	const aside = family.map((member) => [member, `${member}.cfp.tmp`]);
	for (const [member, copy] of aside) fs.copyFileSync(member, copy);
	let folded = false;
	try {
		// Loaded only here: a browser that shut down cleanly leaves no WAL behind.
		const { DatabaseSync } = await import("node:sqlite");
		const db = new DatabaseSync(file);
		try {
			const result = db.prepare("PRAGMA wal_checkpoint(TRUNCATE)").get() as {
				busy: number;
			};
			if (result.busy) {
				throw new Error(
					`another connection holds ${file}, so its WAL cannot be checkpointed`,
				);
			}
			folded = true;
			const rows = db.prepare("PRAGMA quick_check").all() as Record<
				string,
				unknown
			>[];
			return rows.length === 1 && Object.values(rows[0])[0] === "ok";
		} finally {
			db.close();
		}
	} catch (error) {
		if (/another connection holds/.test((error as Error).message)) throw error;
		if (!folded) {
			for (const [member, copy] of aside) fs.copyFileSync(copy, member);
		}
		return false;
	} finally {
		for (const [, copy] of aside) fs.rmSync(copy, { force: true });
	}
}

function sqliteFiles(root: string): string[] {
	const found: string[] = [];
	const visit = (dir: string, rel: string, deep: boolean) => {
		if (!fs.existsSync(dir)) return;
		for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
			const relpath = rel ? `${rel}/${entry.name}` : entry.name;
			if (entry.isDirectory() && deep) {
				visit(path.join(dir, entry.name), relpath, true);
			} else if (entry.isFile() && entry.name.endsWith(".sqlite")) {
				found.push(relpath);
			}
		}
	};
	visit(root, "", false);
	visit(path.join(root, "storage"), "storage", true);
	return found.filter((relpath) => !fileExcluded(relpath)).sort();
}

/**
 * Capture a closed Firefox profile directory in place: fold pending SQLite
 * write-ahead logs, filter prefs.js, drop the launcher's own files, and list
 * what travels.
 */
export async function capture(root: string): Promise<Snapshot> {
	for (const name of LOCK_FILES)
		fs.rmSync(path.join(root, name), { force: true });
	const suspectFiles: string[] = [];
	for (const relpath of sqliteFiles(root)) {
		const file = path.join(root, relpath);
		const wal = `${file}-wal`;
		if (
			fs.existsSync(wal) &&
			fs.statSync(wal).size &&
			!(await checkpoint(file))
		) {
			suspectFiles.push(relpath);
		}
	}
	const forced = new Set(
		suspectFiles.flatMap((relpath) => [`${relpath}-wal`, `${relpath}-shm`]),
	);
	const prefs = path.join(root, "prefs.js");
	if (fs.existsSync(prefs)) {
		const before = fs.readFileSync(prefs);
		const after = filterPrefs(before);
		if (!after.equals(before)) fs.writeFileSync(prefs, after);
	}
	for (const name of DRIVER_OWNED)
		fs.rmSync(path.join(root, name), { force: true });

	const files: CapturedFile[] = [];
	const dirs: string[] = [];
	const walk = (dir: string, rel: string): boolean => {
		let kept = false;
		const entries = fs
			.readdirSync(dir, { withFileTypes: true })
			.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
		for (const entry of entries) {
			const relpath = rel ? `${rel}/${entry.name}` : entry.name;
			const full = path.join(dir, entry.name);
			const info = fs.lstatSync(full);
			if (info.isDirectory()) {
				if (dirExcluded(relpath)) continue;
				if (walk(full, relpath)) kept = true;
				else if (dirKept(relpath)) {
					dirs.push(relpath);
					kept = true;
				}
			} else if (forced.has(relpath) || fileIncluded(relpath, info.size)) {
				if (!info.isFile()) {
					throw new Error(
						`${relpath} is not a regular file; state holds regular files only`,
					);
				}
				files.push({
					path: relpath,
					size: info.size,
					mode: info.mode & 0o777,
					mtime: Math.floor(info.mtimeMs / 1000),
				});
				kept = true;
			}
		}
		return kept;
	};
	walk(root, "");
	const total = files.reduce((sum, file) => sum + file.size, 0);
	if (total > HARD_CAP) {
		throw new StateTooLarge(
			`state_too_large: ${total} bytes over the ${HARD_CAP}-byte cap`,
		);
	}
	return { root, files, dirs, suspectFiles };
}

// ── restore ─────────────────────────────────────────────────────────────────

/** Firefox migrates profiles forward only; opening newer state in an older build corrupts it. */
export class StateNewerThanBrowser extends Error {
	name = "StateNewerThanBrowser";
}

function major(ffVersion: string): number {
	const head = ffVersion.split(".", 1)[0];
	if (!/^\d+$/.test(head))
		throw new Error(`not a Firefox version: ${ffVersion}`);
	return Number(head);
}

/** How many chunks are fetched at once, and so held in memory at once. */
export const WORKERS = 8;

/**
 * Write `manifest` out at `target`, which must not exist. `fetchChunk` returns
 * a chunk's sealed bytes. Nothing lands at `target` unless every chunk opens.
 */
export async function restore(
	manifest: Manifest,
	keys: AccountKeys,
	fetchChunk: (chunkId: string) => Promise<Uint8Array>,
	target: string,
	browserFfVersion: string,
): Promise<void> {
	if (major(manifest.ff_version) > major(browserFfVersion)) {
		throw new StateNewerThanBrowser(
			`state_newer_than_browser: ${manifest.profile_id} v${manifest.version} was written by ` +
				`Firefox ${manifest.ff_version} and cannot be opened by ${browserFfVersion}`,
		);
	}
	if (fs.existsSync(target)) {
		throw new Error(
			`${target} exists; a restore never replaces a profile directory`,
		);
	}
	const staging = path.join(
		path.dirname(target),
		`.${path.basename(target)}.partial`,
	);
	fs.mkdirSync(staging, { mode: 0o700 });
	const handles = new Map<FileEntry, number>();
	try {
		const refs = manifest.files.flatMap((entry) =>
			entry.chunks.map((ref) => ({ entry, ref })),
		);
		for (let start = 0; start < refs.length; start += WORKERS) {
			const batch = refs.slice(start, start + WORKERS);
			const plaintexts = await Promise.all(
				batch.map(async ({ ref }) =>
					keys.openChunk(
						chunkIdBytes(ref.id),
						await fetchChunk(ref.id),
						ref.size,
					),
				),
			);
			batch.forEach(({ entry }, i) => {
				let fd = handles.get(entry);
				if (fd === undefined) {
					const file = path.join(staging, entry.path);
					fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
					fd = fs.openSync(file, "wx", 0o600);
					handles.set(entry, fd);
				}
				fs.writeSync(fd, plaintexts[i]);
			});
		}
		for (const fd of handles.values()) fs.closeSync(fd);
		handles.clear();
		for (const entry of manifest.files) {
			// An empty file has no chunks.
			const file = path.join(staging, entry.path);
			if (!fs.existsSync(file)) {
				fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
				fs.writeFileSync(file, "", { flag: "wx" });
			}
		}
		for (const dir of manifest.dirs) {
			fs.mkdirSync(path.join(staging, dir), { recursive: true, mode: 0o700 });
		}
		const prefs = path.join(staging, "prefs.js");
		if (fs.existsSync(prefs))
			fs.writeFileSync(prefs, filterPrefs(fs.readFileSync(prefs)));
		for (const entry of manifest.files) {
			const file = path.join(staging, entry.path);
			fs.chmodSync(file, entry.mode);
			fs.utimesSync(file, entry.mtime, entry.mtime);
		}
		fs.renameSync(staging, target);
	} catch (error) {
		for (const fd of handles.values()) fs.closeSync(fd);
		fs.rmSync(staging, { recursive: true, force: true });
		throw error;
	}
}
