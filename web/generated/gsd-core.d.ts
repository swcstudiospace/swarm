// world root:component/root
/**
* # Variants
* 
* ## `"input"`
* 
* ## `"timeout"`
* 
* ## `"dep"`
* 
* ## `"capacity"`
* 
* ## `"contract"`
* 
* ## `"policy"`
* 
* ## `"internal"`
*/
export type ErrorCode = 'input' | 'timeout' | 'dep' | 'capacity' | 'contract' | 'policy' | 'internal';
export interface InvokeError {
  code: ErrorCode,
  message: string,
}
export function invoke(input: Uint8Array): Uint8Array;
export type Result<T, E> = { tag: 'ok', val: T } | { tag: 'err', val: E };
