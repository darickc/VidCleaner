import { useParams } from "react-router-dom";
import { Page, Placeholder } from "../components/Page";

export function ItemPage() {
  const { itemId } = useParams();
  return (
    <Page title="Item" subtitle={`Detections and snippets for item ${itemId}.`}>
      <Placeholder milestone="M4">
        Counts per word and category, the detections table, original/clean snippet players, and
        false-positive whitelisting.
      </Placeholder>
    </Page>
  );
}
