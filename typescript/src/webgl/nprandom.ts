/**
 * The slice of numpy's random machinery that `sample_webgl` touches, ported
 * bit-for-bit: `np.random.default_rng(seed)` (SeedSequence -> PCG64),
 * `Generator.random()`, and `Generator.choice(n, p=probs)`, plus the float64
 * `ndarray.sum()` (pairwise) and `cumsum()` the draw normalizes with. The GPU
 * a seeded identity draws in Python is then the GPU it draws here.
 */
import { randomBytes } from "node:crypto";

const MASK64 = (1n << 64n) - 1n;
const MASK128 = (1n << 128n) - 1n;

// numpy/random/bit_generator.pyx
const INIT_A = 0x43b0d7e5;
const MULT_A = 0x931e8875;
const INIT_B = 0x8b51f9dd;
const MULT_B = 0x58f38ded;
const MIX_MULT_L = 0xca01f9dd;
const MIX_MULT_R = 0x4973f715;
const XSHIFT = 16;
const POOL_SIZE = 4;

// PCG_DEFAULT_MULTIPLIER_128
const PCG_MULT = (0x2360ed051fc65da4n << 64n) | 0x4385df649fccf645n;

function hashmix(value: number, hashConst: { v: number }): number {
	value = (value ^ hashConst.v) >>> 0;
	hashConst.v = Math.imul(hashConst.v, MULT_A) >>> 0;
	value = Math.imul(value, hashConst.v) >>> 0;
	value = (value ^ (value >>> XSHIFT)) >>> 0;
	return value;
}

function mix(x: number, y: number): number {
	let result = (Math.imul(MIX_MULT_L, x) - Math.imul(MIX_MULT_R, y)) >>> 0;
	result = (result ^ (result >>> XSHIFT)) >>> 0;
	return result;
}

function intToUint32Array(n: bigint): number[] {
	if (n < 0n) throw new RangeError("expected non-negative integer entropy");
	if (n === 0n) return [0];
	const out: number[] = [];
	while (n > 0n) {
		out.push(Number(n & 0xffffffffn));
		n >>= 32n;
	}
	return out;
}

/** numpy.random.SeedSequence(entropy) with no spawn key. */
export class SeedSequence {
	readonly pool: number[];

	constructor(entropy: number | bigint) {
		const entropyArray = intToUint32Array(BigInt(entropy));
		const mixer = new Array<number>(POOL_SIZE).fill(0);
		const hashConst = { v: INIT_A };
		for (let i = 0; i < POOL_SIZE; i++) {
			mixer[i] =
				i < entropyArray.length
					? hashmix(entropyArray[i], hashConst)
					: hashmix(0, hashConst);
		}
		for (let src = 0; src < POOL_SIZE; src++) {
			for (let dst = 0; dst < POOL_SIZE; dst++) {
				if (src !== dst)
					mixer[dst] = mix(mixer[dst], hashmix(mixer[src], hashConst));
			}
		}
		for (let src = POOL_SIZE; src < entropyArray.length; src++) {
			for (let dst = 0; dst < POOL_SIZE; dst++) {
				mixer[dst] = mix(mixer[dst], hashmix(entropyArray[src], hashConst));
			}
		}
		this.pool = mixer;
	}

	/** generate_state(nWords, np.uint64) */
	generateState64(nWords: number): bigint[] {
		const n32 = nWords * 2;
		const state: number[] = [];
		let hashConst = INIT_B;
		for (let i = 0; i < n32; i++) {
			let dataVal = this.pool[i % POOL_SIZE];
			dataVal = (dataVal ^ hashConst) >>> 0;
			hashConst = Math.imul(hashConst, MULT_B) >>> 0;
			dataVal = Math.imul(dataVal, hashConst) >>> 0;
			dataVal = (dataVal ^ (dataVal >>> XSHIFT)) >>> 0;
			state.push(dataVal);
		}
		const out: bigint[] = [];
		for (let i = 0; i < nWords; i++) {
			out.push((BigInt(state[2 * i + 1]) << 32n) | BigInt(state[2 * i]));
		}
		return out;
	}
}

/** numpy.random.PCG64 (XSL-RR 128/64). */
export class PCG64 {
	private state = 0n;
	private inc = 0n;

	constructor(seed: number | bigint | null | undefined) {
		const entropy =
			seed === null || seed === undefined
				? BigInt(`0x${randomBytes(16).toString("hex")}`)
				: seed;
		const [s0, s1, i0, i1] = new SeedSequence(entropy).generateState64(4);
		const initstate = (s0 << 64n) | s1;
		const initseq = (i0 << 64n) | i1;
		this.inc = ((initseq << 1n) | 1n) & MASK128;
		this.step();
		this.state = (this.state + initstate) & MASK128;
		this.step();
	}

	private step(): void {
		this.state = (this.state * PCG_MULT + this.inc) & MASK128;
	}

	next64(): bigint {
		this.step();
		const hi = this.state >> 64n;
		const lo = this.state & MASK64;
		const xored = hi ^ lo;
		const rot = hi >> 58n;
		return ((xored >> rot) | (xored << ((64n - rot) & 63n))) & MASK64;
	}

	/** next_double: 53 random bits in [0, 1). */
	nextDouble(): number {
		return Number(this.next64() >> 11n) * (1.0 / 9007199254740992.0);
	}
}

/** np.random.default_rng(seed) -- the methods sample_webgl uses. */
export class NumpyGenerator {
	private readonly bitGen: PCG64;

	constructor(seed?: number | bigint | null) {
		this.bitGen = new PCG64(seed);
	}

	random(): number {
		return this.bitGen.nextDouble();
	}

	/** Generator.choice(p.length, p=p) with a scalar result. */
	choiceIndex(p: number[]): number {
		const cdf = cumsum(p);
		const last = cdf[cdf.length - 1];
		for (let i = 0; i < cdf.length; i++) cdf[i] /= last;
		const u = this.random();
		// searchsorted(side='right'): the count of cdf values <= u
		let lo = 0;
		let hi = cdf.length;
		while (lo < hi) {
			const mid = (lo + hi) >>> 1;
			if (u < cdf[mid]) hi = mid;
			else lo = mid + 1;
		}
		return lo;
	}
}

/** ndarray.cumsum() on float64 (sequential). */
export function cumsum(values: number[]): number[] {
	const out: number[] = [];
	let acc = 0;
	values.forEach((v, i) => {
		acc = i === 0 ? v : acc + v;
		out.push(acc);
	});
	return out;
}

const PW_BLOCKSIZE = 128;

function pairwiseSum(a: number[], start: number, n: number): number {
	if (n < 8) {
		let res = 0;
		for (let i = 0; i < n; i++) res += a[start + i];
		return res;
	}
	if (n <= PW_BLOCKSIZE) {
		const r = a.slice(start, start + 8);
		let i = 8;
		for (; i < n - (n % 8); i += 8) {
			for (let j = 0; j < 8; j++) r[j] += a[start + i + j];
		}
		let res = r[0] + r[1] + (r[2] + r[3]) + (r[4] + r[5] + (r[6] + r[7]));
		for (; i < n; i++) res += a[start + i];
		return res;
	}
	let n2 = Math.floor(n / 2);
	n2 -= n2 % 8;
	return pairwiseSum(a, start, n2) + pairwiseSum(a, start + n2, n - n2);
}

/** ndarray.sum() on a contiguous 1-D float64 array (numpy's pairwise sum). */
export function npSum(values: number[]): number {
	return 0 + pairwiseSum(values, 0, values.length);
}
