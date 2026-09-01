import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { ItemPage } from "./pages/Item";
import { LibraryPage } from "./pages/Library";
import { QueuePage } from "./pages/Queue";
import { SettingsPage } from "./pages/Settings";
import { TitlePage } from "./pages/Title";
import { WordsPage } from "./pages/Words";

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<QueuePage />} />
        <Route path="library" element={<LibraryPage />} />
        <Route path="titles/:titleId" element={<TitlePage />} />
        <Route path="items/:itemId" element={<ItemPage />} />
        <Route path="words" element={<WordsPage />} />
        <Route path="settings" element={<SettingsPage />} />
      </Route>
    </Routes>
  );
}
