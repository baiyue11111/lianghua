import {useEffect,useState} from 'react';
import {createDisplay} from '@display/core';
import type {BackendPort} from '@display/core';
import {features} from '@display/features';
import {DisplayProvider} from './interaction/model';
import Workspace from './Workspace';
export default function App({backend}:{backend?:BackendPort}) {
 const [display]=useState(()=>createDisplay({...features(),backend}));
 useEffect(()=>()=>queueMicrotask(()=>display.dispose()),[display]);
 return <DisplayProvider value={display}><Workspace/></DisplayProvider>;
}
