import {createRoot} from 'react-dom/client';
import './index.css';
import App from './App';
import {createBackend} from './backend';

const backend=createBackend();
createRoot(document.getElementById('root')!).render(<App backend={backend}/>);
