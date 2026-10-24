import {vi} from 'vitest';
const {webcrypto}=await vi.importActual<{webcrypto:Crypto}>('node:crypto');
/** Real SHA-256 plus an exclusive, ifAvailable lock manager for isolated UI tests. */
export function installInspectionSubmissionRuntime(){
  vi.stubGlobal('crypto',webcrypto);
  const descriptor=Object.getOwnPropertyDescriptor(navigator,'locks'),held=new Set<string>();
  Object.defineProperty(navigator,'locks',{configurable:true,value:{request:async(name:string,_options:unknown,callback:(lock:unknown)=>unknown)=>{
    if(held.has(name))return callback(null);held.add(name);try{return await callback({name});}finally{held.delete(name);}
  }}});
  return()=>{if(descriptor)Object.defineProperty(navigator,'locks',descriptor);else delete (navigator as unknown as {locks?:unknown}).locks;vi.unstubAllGlobals();};
}
