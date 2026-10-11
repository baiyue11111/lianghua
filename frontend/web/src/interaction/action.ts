import type {UiAction,JsonValue} from '@display/core';
let serial=0;
export function action(kind:UiAction['kind'],target:string,data:unknown,operation?:string):UiAction {
 return {action_id:`web-${++serial}`,kind,target,data:JSON.parse(JSON.stringify(data)) as JsonValue,...(operation?{operation}:{})};
}
