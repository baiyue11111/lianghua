// Server-rendered component events, using the real React hooks and display core.
import {createElement,Fragment} from 'react';
export {Fragment};
export const nodes=[];
export function capture(type,props,...children){
  if(typeof type==='string'||props?.option)nodes.push({type,props:props??{},children});
  return createElement(type,props,...children);
}
