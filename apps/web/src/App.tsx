/**
 * The application shell (SPEC §57).
 *
 * §57 lists nine screens; the navigation between them is not free-form. Import
 * comes before Analysis because there is nothing to analyse, Moments before
 * Timeline because the edit is made of them, and Preview after Render because
 * there is no file until then. The shell therefore knows which screens are
 * *reachable* and says why the others are not, rather than offering a tab that
 * shows an empty page.
 *
 * No router: the whole application is one project at a time, and a URL scheme
 * would be a second source of truth about which screen is open.
 */

import {useCallback, useEffect, useState} from 'react';

import {ChatPanel} from './components/ChatPanel';
import {AnalysisScreen} from './screens/AnalysisScreen';
import {DashboardScreen} from './screens/DashboardScreen';
import {ExportScreen} from './screens/ExportScreen';
import {ImportScreen} from './screens/ImportScreen';
import {MomentsScreen} from './screens/MomentsScreen';
import {PreviewScreen} from './screens/PreviewScreen';
import {TimelineScreen} from './screens/TimelineScreen';
import {api, type Project} from './lib/api';

export type ScreenName =
  | 'dashboard'
  | 'import'
  | 'analysis'
  | 'moments'
  | 'timeline'
  | 'preview'
  | 'export';

const SCREENS: ReadonlyArray<{name: ScreenName; label: string; needsProject: boolean}> = [
  {name: 'dashboard', label: 'Dashboard', needsProject: false},
  {name: 'import', label: 'Import', needsProject: true},
  {name: 'analysis', label: 'Analysis', needsProject: true},
  {name: 'moments', label: 'Moments', needsProject: true},
  {name: 'timeline', label: 'Timeline', needsProject: true},
  {name: 'preview', label: 'Preview', needsProject: true},
  {name: 'export', label: 'Export', needsProject: true},
];

export function App() {
  const [screen, setScreen] = useState<ScreenName>('dashboard');
  const [project, setProject] = useState<Project | null>(null);
  const [chatOpen, setChatOpen] = useState(false);

  const openProject = useCallback((next: Project) => {
    setProject(next);
    setScreen('import');
  }, []);

  // Keep the header's project details fresh: the mode and target duration can
  // be changed from the Import screen, and the header shows both.
  const refresh = useCallback(async () => {
    if (!project) return;
    try {
      setProject(await api.projects.get(project.id));
    } catch {
      /* a project deleted elsewhere just stops refreshing */
    }
  }, [project]);

  useEffect(() => {
    document.title = project ? `${project.name} — AI Gaming Video Editor` : 'AI Gaming Video Editor';
  }, [project]);

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-title">
          <strong>AI Gaming Video Editor</strong>
          {project && (
            <span className="app-project">
              {project.name}
              <span className="muted">
                {' · '}
                {Math.round(project.target_duration_seconds / 60)} min · {project.mode.replace('_', ' ')}
              </span>
            </span>
          )}
        </div>
        <nav className="app-nav">
          {SCREENS.map((item) => {
            const disabled = item.needsProject && !project;
            return (
              <button
                key={item.name}
                type="button"
                className={screen === item.name ? 'nav-item active' : 'nav-item'}
                disabled={disabled}
                title={disabled ? 'Open a project first' : undefined}
                onClick={() => setScreen(item.name)}
              >
                {item.label}
              </button>
            );
          })}
        </nav>
        {project && (
          <button type="button" className="chat-toggle" onClick={() => setChatOpen((open) => !open)}>
            {chatOpen ? 'Close chat' : 'Ask about your video…'}
          </button>
        )}
      </header>

      <main className="app-main">
        {screen === 'dashboard' && <DashboardScreen onOpen={openProject} />}
        {screen === 'import' && project && (
          <ImportScreen project={project} onChanged={refresh} onAnalyse={() => setScreen('analysis')} />
        )}
        {screen === 'analysis' && project && (
          <AnalysisScreen project={project} onDone={() => setScreen('moments')} />
        )}
        {screen === 'moments' && project && <MomentsScreen project={project} />}
        {screen === 'timeline' && project && (
          <TimelineScreen project={project} onRender={() => setScreen('export')} />
        )}
        {screen === 'preview' && project && <PreviewScreen project={project} />}
        {screen === 'export' && project && (
          <ExportScreen project={project} onPreview={() => setScreen('preview')} />
        )}
      </main>

      {project && chatOpen && <ChatPanel project={project} onClose={() => setChatOpen(false)} />}
    </div>
  );
}
