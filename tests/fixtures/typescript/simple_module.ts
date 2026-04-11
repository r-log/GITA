import { helper } from './helper';
import type { Config } from './types';

const GREETING = 'hello';

export function add(a: number, b: number): number {
  return a + b;
}

function multiply(a: number, b: number): number {
  return a * b;
}

export async function fetchData(url: string): Promise<string | null> {
  return null;
}
