import { useParams } from "react-router-dom";
import { Page, Placeholder } from "../components/Page";

export function TitlePage() {
  const { titleId } = useParams();
  return (
    <Page title="Title" subtitle={`Episodes and per-word rollups for title ${titleId}.`}>
      <Placeholder milestone="M4">
        Episode list with status badges, per-word counts, and process / reprocess / restore actions.
      </Placeholder>
    </Page>
  );
}
