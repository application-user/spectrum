import pandas as pd
import numpy as np


def majority_vote(claims):
    """perform truth discovery using majority voting
   
    Parameters
    ----------
    claims: pd.DataFrame
        a data frame that has columns [source_id, object_id, value]
       
    Returns
    -------
    discovered_truths: pd.DataFrame
        a data frame that has [object_id, value]
    """
    c_df = claims[['source_id', 'object_id', 'value']].copy()
    discovered_truths = c_df.groupby(['object_id']).apply(lambda x: elect(x))
    discovered_truths = pd.DataFrame(discovered_truths)
    discovered_truths = discovered_truths.rename(columns={
        0: 'value'
    }).reset_index()
    return discovered_truths


def elect(x):
    """compute the truth value based on voting; the value received the most votes (by sources) is returned
    
    Parameters
    ----------
    x: pd.DataFrame
    
    Returns
    -------
    discovered_truth: pd.DataFrame
        the discovered truth
    """
    return x.value.value_counts().idxmax()